#!/bin/bash
#SBATCH --job-name=c_rec
#SBATCH --output=c_rec.txt
#SBATCH --time=48:00:00
#SBATCH --gres gpu:1
#SBATCH --qos=batch
#SBATCH --mem=32G
#SBATCH --constraint=gpu_12gb

module purge
module load python-3.5 cuda-8.0

USE_CUDA=1 python3 -u model.py --modelnameload c_rec --modelnamesave c_rec --server 1 --lambda_reconstruct 0
