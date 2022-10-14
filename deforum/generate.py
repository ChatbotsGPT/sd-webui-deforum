import torch
from PIL import Image, ImageOps
import requests
import numpy as np
import torchvision.transforms.functional as TF
from pytorch_lightning import seed_everything
import os
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.ddim import DDIMSampler
from k_diffusion.external import CompVisDenoiser
from torch import autocast
from contextlib import nullcontext
from einops import rearrange

from .prompt import get_uc_and_c, parse_weight
from .k_samplers import sampler_fn
from scipy.ndimage import gaussian_filter

from .callback import SamplerCallback

#Webui
from modules import processing
from modules.processing import process_images

def add_noise(sample: torch.Tensor, noise_amt: float) -> torch.Tensor:
    return sample + torch.randn(sample.shape, device=sample.device) * noise_amt

def load_img(path, shape, use_alpha_as_mask=False):
    # use_alpha_as_mask: Read the alpha channel of the image as the mask image
    if path.startswith('http://') or path.startswith('https://'):
        image = Image.open(requests.get(path, stream=True).raw)
    else:
        image = Image.open(path)

    if use_alpha_as_mask:
        image = image.convert('RGBA')
    else:
        image = image.convert('RGB')

    image = image.resize(shape, resample=Image.LANCZOS)

    mask_image = None
    if use_alpha_as_mask:
        # Split alpha channel into a mask_image
        red, green, blue, alpha = Image.Image.split(image)
        mask_image = alpha.convert('L')
        image = image.convert('RGB')

    return image, mask_image #PIL image for auto's pipeline

def load_mask_latent(mask_input, shape):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    
    if isinstance(mask_input, str): # mask input is probably a file name
        if mask_input.startswith('http://') or mask_input.startswith('https://'):
            mask_image = Image.open(requests.get(mask_input, stream=True).raw).convert('RGBA')
        else:
            mask_image = Image.open(mask_input).convert('RGBA')
    elif isinstance(mask_input, Image.Image):
        mask_image = mask_input
    else:
        raise Exception("mask_input must be a PIL image or a file name")

    mask_w_h = (shape[-1], shape[-2])
    mask = mask_image.resize(mask_w_h, resample=Image.LANCZOS)
    mask = mask.convert("L")
    return mask

def prepare_mask(mask_input, mask_shape, mask_brightness_adjust=1.0, mask_contrast_adjust=1.0, invert_mask=False):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    # mask_brightness_adjust (non-negative float): amount to adjust brightness of the iamge, 
    #     0 is black, 1 is no adjustment, >1 is brighter
    # mask_contrast_adjust (non-negative float): amount to adjust contrast of the image, 
    #     0 is a flat grey image, 1 is no adjustment, >1 is more contrast
    
    mask = load_mask_latent(mask_input, mask_shape)

    # Mask brightness/contrast adjustments
    if mask_brightness_adjust != 1:
        mask = TF.adjust_brightness(mask, mask_brightness_adjust)
    if mask_contrast_adjust != 1:
        mask = TF.adjust_contrast(mask, mask_contrast_adjust)

    if invert_mask:
        mask = PIL.ImageOps.invert(mask)
    
    return mask
    
def generate(args, root, frame = 0, return_sample=False):
    import re
    assert args.prompt is not None
    
    # Evaluate prompt math!
    
    math_parser = re.compile("""
            (?P<weight>(
            `[\S\s]*?`# a math function wrapped in `-characters
            ))
            """, re.VERBOSE)
    
    parsed_prompt = re.sub(math_parser, lambda m: str(parse_weight(m, frame)), args.prompt)
    
    # Setup the pipeline
    p = root.p
    
    os.makedirs(args.outdir, exist_ok=True)
    p.batch_size = args.n_samples
    p.width = args.W
    p.height = args.H
    p.steps = args.steps
    p.seed = args.seed
    p.do_not_save_samples = not args.save_samples
    p.do_not_save_grid = not args.make_grid
    p.sampler_index = int(args.sampler)
    p.mask_blur = args.mask_overlay_blur
    p.extra_generation_params["Mask blur"] = args.mask_overlay_blur
    p.n_iter = 1
    p.cfg_scale = args.scale
    p.outpath_samples = root.outpath_samples
    p.outpath_grids = root.outpath_samples
    
    prompt_split = parsed_prompt.split("--neg")
    if len(prompt_split) > 1:
        p.prompt, p.negative_prompt = parsed_prompt.split("--neg") #TODO: add --neg to vanilla Deforum for compat
    else:
        p.prompt = prompt_split[0]
        p.negative_prompt = ""
    
    if not args.use_init and args.strength > 0 and args.strength_0_no_init:
        print("\nNo init image, but strength > 0. Strength has been auto set to 0, since use_init is False.")
        print("If you want to force strength > 0 with no init, please set strength_0_no_init to False.\n")
        args.strength = 0
    p.denoising_strength = args.strength
    mask_image = None
    init_image = None
    
    if args.init_sample is not None:
        # Converts to PIL, but 
        args.init_sample = 255. * rearrange(args.init_sample.cpu().numpy(), 'c h w -> h w c')
        init_image = Image.fromarray(args.init_sample.astype(np.uint8))
    elif args.use_init and args.init_image != None and args.init_image != '':
        init_image, mask_image = load_img(args.init_image, 
                                          shape=(args.W, args.H),  
                                          use_alpha_as_mask=args.use_alpha_as_mask)
        #init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
    else:
        #random noise
        a = np.random.rand(args.W, args.H, 3)*255
        init_image = Image.fromarray(a.astype('uint8')).convert('RGB')
    
    # Mask functions
    if args.use_mask:
        assert args.mask_file is not None or mask_image is not None, "use_mask==True: An mask image is required for a mask. Please enter a mask_file or use an init image with an alpha channel"
        assert args.use_init, "use_mask==True: use_init is required for a mask"


        mask = prepare_mask(args.mask_file if mask_image is None else mask_image, 
                            init_image.shape, 
                            args.mask_contrast_adjust, 
                            args.mask_brightness_adjust,
                            args.invert_mask)
        
        #if (torch.all(mask == 0) or torch.all(mask == 1)) and args.use_alpha_as_mask:
        #    raise Warning("use_alpha_as_mask==True: Using the alpha channel from the init image as a mask, but the alpha channel is blank.")
        
        #mask = repeat(mask, '1 ... -> b ...', b=batch_size)
    else:
        mask = None

    assert not ( (args.use_mask and args.overlay_mask) and (args.init_sample is None and init_image is None)), "Need an init image when use_mask == True and overlay_mask == True"
    
    p.init_images = [init_image]
    p.mask = mask
    
    processed = processing.process_images(p)
    
    if root.initial_info == None:
        root.initial_seed = processed.seed
        root.initial_info = processed.info
    
    if root.first_frame == None:
        root.first_frame = processed.images[0]
    
    if return_sample:
        image = np.array(image).astype(np.float16) / 255.0
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image)
        image = 2.*image - 1.
        results = [image, process_images[0]]
    else:
        results = [processed.images[0]]
    
    return results