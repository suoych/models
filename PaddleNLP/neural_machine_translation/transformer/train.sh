# open garbage collection to save memory
export FLAGS_eager_delete_tensor_gb=0.0
# setting visible devices for training
export CUDA_VISIBLE_DEVICES=0
export FLAGS_fraction_of_gpu_memory_to_use=0 

python -u main.py \
  --do_train True \
  --epoch 1 \
  --src_vocab_fpath wmt16_ende_data_bpe_clean/vocab_all.bpe.32000 \
  --trg_vocab_fpath wmt16_ende_data_bpe_clean/vocab_all.bpe.32000 \
  --special_token '<s>' '<e>' '<unk>' \
  --training_file wmt16_ende_data_bpe_clean/train.tok.clean.bpe.32000.en-de \
  --batch_size 35000
