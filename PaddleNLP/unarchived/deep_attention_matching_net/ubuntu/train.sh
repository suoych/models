export CUDA_VISIBLE_DEVICES=1
export FLAGS_eager_delete_tensor_gb=0  # enable gc
export FLAGS_memory_fraction_of_eager_deletion=1
export FLAGS_fraction_of_gpu_memory_to_use=0.999
~/paddle_release_home/python/bin/python -u ../train_and_evaluate.py --use_cuda \
                --data_path ./data/data.pkl \
                --word_emb_init ./data/word_embedding.pkl \
                --save_path ./models \
                --use_pyreader \
                --batch_size 1650 \
                --vocab_size 434512 \
                --emb_size 200 \
                --_EOS_ 28270
                
