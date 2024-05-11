from transformers import Trainer, TrainingArguments, BitsAndBytesConfig
from transformers.hf_argparser import HfArgumentParser
from dataclasses import dataclass, field
import torch
import os
import wandb
import regex as re
from train_utils import get_peft_state_maybe_zero_3, get_peft_state_non_lora_maybe_zero_3, find_all_linear_names
from conversation import conv_idefics_2 as default_conv, conv_templates
from mantis.train.data import load_data, load_data_from_config, set_ignore_index
from pathlib import Path
from typing import Optional
from pathlib import Path

os.environ["WANDB_RESUME"] = "allow"
os.environ["WANDB_RUN_ID"] = wandb.util.generate_id()
os.environ["TOKENIZERS_PARALLELISM"] = "false"
torch.set_printoptions(profile="full")

# The flag below controls whether to allow TF32 on matmul. This flag defaults to False
# in PyTorch 1.12 and later.
torch.backends.cuda.matmul.allow_tf32 = True

# The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.backends.cudnn.allow_tf32 = True
torch.backends.cuda.enable_flash_sdp(True)

@dataclass
class DataArguments:
    train_data_file: Optional[str] = field(
        metadata={"help": "The input training data file (a text file).", "default": None, "required": False},
        default=None,
    )
    val_data_file: Optional[str] = field(
        metadata={"help": "An optional input validation data file (a text file).", "default": None, "required": False},
        default=None,
    )
    test_data_file: Optional[str] = field(
        metadata={"help": "An optional input test data file (a text file).", "default": None, "required": False},
        default=None,
    )
    data_format: Optional[str] = field(
        metadata={"help": "The format of the data file", "default": "chat", "choices": ["chat", "vqa"]},
        default="chat",
    )
    max_seq_len: Optional[int] = field(
        metadata={"help": "The maximum total input sequence length after tokenization. Sequences longer "
                          "than this will be truncated.", "default": 1024, "required": False},
        default=1024,
    )
    data_config_file: Optional[str] = field(
        metadata={"help": "Pretrained config name or path if not the same as model_name", "default": None, "required": False},
        default=None,
    )
    dataset_balancing: Optional[bool] = field(
        metadata={"help": "Whether to balance the dataset", "default": True, "required": False},
        default=True,
    )

@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models", "default": "HuggingFaceM4/idefics2-8b", "required": False},
        default="HuggingFaceM4/idefics2-8b",
    )
    lora_enabled: Optional[bool] = field(
        metadata={"help": "Whether to use LoRA", "default": False, "required": False},
        default=False,
    )
    qlora_enabled: Optional[bool] = field(
        metadata={"help": "Whether to use QLoRA", "default": False, "required": False},
        default=False,
    )
    lora_r: Optional[int] = field(
        metadata={"help": "LoRA r", "default": 8, "required": False},
        default=8,
    )
    lora_alpha: Optional[float] = field(
        metadata={"help": "LoRA alpha", "default": 8, "required": False},
        default=8,
    )
    lora_dropout: Optional[float] = field(
        metadata={"help": "LoRA dropout", "default": 0.1, "required": False},
        default=0.1,
    )
    lora_bias: Optional[str] = field(
        metadata={"help": "LoRA bias", "default": 'none', "required": False},
        default='none',
    )
    attn_implementation: Optional[str] = field(
        metadata={"help": "The attention implementation to use", "default": "flash_attention_2", "required": False},
        default="flash_attention_2",
    )
    max_image_size: Optional[str] = field(
        metadata={"help": "The maximum image size", "default": "(1080,1920)", "required": False},
        default="(1080,1920)",
    )
    tune_xatten_layer_only: Optional[bool] = field(
        metadata={"help": "Whether to tune only the x-attention layer", "default": False, "required": False},
        default=False,
    )
    do_pretrain: Optional[bool] = field(
        metadata={"help": "Whether to pretrain the projector", "default": False, "required": False},
        default=False,
    )
    llm_backbone: Optional[str] = field(
        metadata={"help": "The LLM backbone to use", "default": "meta-llama/Meta-Llama-3-8B", "required": False},
        default="meta-llama/Meta-Llama-3-8B",
    )
    vision_backbone: Optional[str] = field(
        metadata={"help": "The vision backbone to use", "default": "openai/clip-vit-large-patch14-336", "required": False},
        default="openai/clip-vit-large-patch14-336",
    )
    conv_template: Optional[str] = field(
        metadata={"help": "The conversation template to use", "default": None, "required": False},
        default=None,
    )
    projector : Optional[str] = field(
        metadata={"help": "The projector from vision to LLM", "default": "MLP", "required": False},
        default="MLP",
    )
    

def load_model(model_args, training_args):
    print("Loading model...")
    torch_dtype = torch.bfloat16 if training_args.bf16 else torch.float16 if training_args.fp16 else torch.float32
    from transformers import Idefics2ForConditionalGeneration, Idefics2Processor
    processor = Idefics2Processor.from_pretrained(model_args.model_name_or_path)
    # processor.image_processor.do_image_splitting = False
    
    if model_args.lora_enabled:
        from peft import LoraConfig
        lora_config = LoraConfig(
            r=model_args.lora_r,
            lora_alpha=model_args.lora_alpha,
            target_modules='.*(text_model|modality_projection|perceiver_resampler).*(down_proj|gate_proj|up_proj|k_proj|q_proj|v_proj|o_proj).*$',
            lora_dropout=model_args.lora_dropout,
            use_dora=False if model_args.qlora_enabled else True,
            init_lora_weights="gaussian"
        )
        if model_args.qlora_enabled:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16
            )
        model = Idefics2ForConditionalGeneration.from_pretrained(
            model_args.model_name_or_path,
            torch_dtype=torch.float16,
            quantization_config=bnb_config if model_args.qlora_enabled else None,
        )
        model.add_adapter(lora_config)
        model.enable_adapters()
        print("Successfully loaded lora model from:", model_args.model_name_or_path)
    else:
        model = Idefics2ForConditionalGeneration.from_pretrained(
            model_args.model_name_or_path, torch_dtype=torch_dtype,
            attn_implementation = model_args.attn_implementation
        )
        print("Successfully loaded model from:", model_args.model_name_or_path)
    
    # idefics2's ignore index is not -100.
    set_ignore_index(model.image_token_id)
        
    return model, processor
    
def main(
    training_args: TrainingArguments,
    data_args: DataArguments,
    model_args: ModelArguments,
):
    if model_args.do_pretrain:
        training_args.output_dir = Path(training_args.output_dir) / model_args.llm_backbone.split("/")[-1] / training_args.run_name
    else:
        training_args.output_dir = Path(training_args.output_dir) / model_args.model_name_or_path.split("/")[-1] / training_args.run_name
    
    training_args.output_dir.mkdir(parents=True, exist_ok=True)
    training_args.output_dir = str(training_args.output_dir)
    training_args.remove_unused_columns = False
    data_args.is_master_worker = training_args.local_rank in [-1, 0]
    
    if not training_args.resume_from_checkpoint:
        training_args.resume_from_checkpoint = True
    if training_args.resume_from_checkpoint == True:
        # search for the latest checkpoint
        all_checkpoints = list(Path(training_args.output_dir).glob("checkpoint-*"))
        if len(all_checkpoints) == 0:
            training_args.resume_from_checkpoint = None
            print("No checkpoint found, starting from scratch")
        else:
            all_checkpoints = [str(x) for x in all_checkpoints]
            latest_checkpoint = max(all_checkpoints, key=os.path.getctime)
            training_args.resume_from_checkpoint = latest_checkpoint
            print("Resuming from checkpoint", latest_checkpoint)
    
    model, processor = load_model(model_args, training_args)
    
    if model_args.conv_template:
        data_args.conv_format = conv_templates[model_args.conv_template] 
    else:
        data_args.conv_format = conv_templates['idefics_2']
    print("Using conversation template:", data_args.conv_format)
    if data_args.data_config_file is not None:
        train_dataset, val_dataset, test_dataset, collate_fn = load_data_from_config(data_args, processor)
    else:
        train_dataset, val_dataset, test_dataset, collate_fn = load_data(data_args, processor)
    
    
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collate_fn,
        tokenizer=processor
    )
    if trainer.is_world_process_zero():
        print("Training arguments:")
        print(training_args)
        print("Data arguments:")
        print(data_args)
        print("Model arguments:")
        print(model_args)
    if training_args.do_train:
        print("Training model...")
        trainer.train(resume_from_checkpoint=training_args.resume_from_checkpoint)
        # save
        final_checkpoint_dir = os.path.join(training_args.output_dir, 'checkpoint-final')
        if model_args.lora_enabled:
            state_dict = get_peft_state_maybe_zero_3(
                model.named_parameters(), model_args.lora_bias
            )
            non_lora_state_dict = get_peft_state_non_lora_maybe_zero_3(
                model.named_parameters()
            )
            if training_args.local_rank == 0 or training_args.local_rank == -1:
                model.config.save_pretrained(final_checkpoint_dir)
                model.save_pretrained(final_checkpoint_dir, state_dict=state_dict)
                torch.save(non_lora_state_dict, os.path.join(final_checkpoint_dir, 'non_lora_trainables.bin'))
        else:
            trainer.save_model(output_dir=final_checkpoint_dir)
        processor.save_pretrained(final_checkpoint_dir)
    if training_args.do_predict:
        print("Predicting...")
        trainer.predict(test_dataset)


if __name__ == "__main__":
    parser = HfArgumentParser((TrainingArguments, DataArguments, ModelArguments))
    training_args, data_args, model_args = parser.parse_args_into_dataclasses()

    main(training_args, data_args, model_args)