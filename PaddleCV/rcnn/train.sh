export CUDA_VISIBLE_DEVICES=0

python train.py \
   --model_save_dir=output/ \
   --pretrained_model=pretrained/imagenet_resnet50_fusebn \
   --data_dir=dataset/coco \
   --MASK_ON=True
