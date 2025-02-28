export CUDA_VISIBLE_DEVICES=1

python train.py \
   --model_save_dir=output/ \
   --pretrain=weights/darknet53 \
   --data_dir=dataset/coco \
   --class_num=80
