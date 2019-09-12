export CUDA_VISIBLE_DEVICES=0

~/paddle_release_home/python/bin/python train.py \
   --model_save_dir=output/ \
   --pretrained_model=pretrained/imagenet_resnet50_fusebn \
   --data_dir=dataset/coco \
   --MASK_ON=True
