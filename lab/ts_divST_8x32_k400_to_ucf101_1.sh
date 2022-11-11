#!/bin/bash

#SBATCH --job-name=svt_ts_divST_8x32_k400_to_ucf101_1
#SBATCH --gres=gpu:4
#SBATCH -o /data/junbum766/repo/svt/lab/output/svt_ts_divST_8x32_k400_to_ucf101_1.out
#SBATCH --error=/data/junbum766/repo/svt/lab/output/svt_ts_divST_8x32_k400_to_ucf101_1.err
#SBATCH -p batch
#SBATCH --cpus-per-gpu=4
#SBATCH --mem-per-gpu=20G
#SBATCH --time 3-0
#SBATCH --partition batch_ce_ugrad

source /data/junbum766/init.sh
conda activate svt

bash /data/junbum766/repo/svt/scripts/eval_linear.sh