import os
USE_IMAGENET_PRETRAINED = True # otherwise use detectron, but that doesnt seem to work?!?

# Change these to match where your annotations and images are
# VCR_IMAGES_DIR = os.path.join(os.path.dirname(__file__), 'data', 'vcr1images')
VCR_IMAGES_DIR = '/root/autodl-tmp/vcr1images'
VCR_ANNOTS_DIR: str = '/root/autodl-tmp/vcr1annots'
DATALOADER_DIR = '/tmp/HGL-pytorch-master'
BERT_DIR = '/root/autodl-tmp/bert'


# VCR_ANNOTS_DIR = os.path.join(os.path.dirname(__file__), 'data')

if not os.path.exists(VCR_IMAGES_DIR):
    raise ValueError("Update config.py with where you saved VCR images to.")
