#!/bin/bash

PROJECT_PATH="/data/junbum766/repo/svt"
EXP_NAME="ts_divST_8x32_k400_to_ucf101_finetune_1"
DATASET="ucf101"
CHECKPOINT="/data/junbum766/repo/svt/lab/checkpoints/kinetics400_vitb_ssl.pth"

cd "$PROJECT_PATH" || exit

if [ ! -d "/data/junbum766/repo/svt/lab/checkpoints/$EXP_NAME" ]; then
  mkdir "/data/junbum766/repo/svt/lab/checkpoints/$EXP_NAME"
fi

export CUDA_VISIBLE_DEVICES=0,1,2,3

python -m torch.distributed.launch \
  --nproc_per_node=4 \
  --master_port="$RANDOM" \
  fine_tune.py \
  --n_last_blocks 1 \
  --arch "vit_base" \
  --pretrained_weights "$CHECKPOINT" \
  --epochs 15 \
  --lr 0.064 \
  --batch_size_per_gpu 8 \
  --num_workers 8 \
  --num_labels 101 \
  --dataset "$DATASET" \
  --output_dir "/data/junbum766/repo/svt/lab/checkpoints/$EXP_NAME" \
  --cfg "/data/junbum766/repo/svt/models/configs/ucf101/TimeSformer_divST_8x32_224_ucf101.yaml" \
  --opts \
  DATA.PATH_TO_DATA_DIR "/data/junbum766/repo/svt/lab/split/ucf101" \
  DATA.PATH_PREFIX "/local_datasets/ucf101/videos" \
  DATA.USE_FLOW False
