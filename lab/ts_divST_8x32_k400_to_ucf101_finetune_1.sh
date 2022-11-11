#!/bin/bash

#SBATCH --job-name=ts_divST_8x32_k400_to_ucf101_finetune_1
#SBATCH --gres=gpu:4
#SBATCH -o /data/junbum766/repo/svt/lab/output/ts_divST_8x32_k400_to_ucf101_finetune_1.out
#SBATCH --error=/data/junbum766/repo/svt/lab/output/ts_divST_8x32_k400_to_ucf101_finetune_1.err
#SBATCH -p batch
#SBATCH --cpus-per-gpu=4
#SBATCH --mem-per-gpu=20G
#SBATCH --time 3-0
#SBATCH --partition batch_ce_ugrad

source /data/junbum766/init.sh
conda activate svt

bash /data/junbum766/repo/svt/scripts/fine_tune.sh