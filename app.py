import torch
import numpy as np
from PIL import Image
import gradio as gr

import os
import piexif
import json
from datetime import datetime


from diffusers import FluxTransformer2DModel, FluxPipeline
from transformers import T5EncoderModel, CLIPTextModel
from diffusers import FluxInpaintPipeline, AutoencoderKL
from src.pipeline_tryon import FluxTryonPipeline
# from optimum.quanto import freeze, qfloat8, quantize

device = torch.device("cuda")
torch_dtype = torch.bfloat16

def load_models(device=device, torch_dtype=torch_dtype,):
    bfl_repo = "black-forest-labs/FLUX.1-schnell"
    # Enable memory efficient attention
    text_encoder = CLIPTextModel.from_pretrained(bfl_repo, subfolder="text_encoder", torch_dtype=torch_dtype,)
    text_encoder_2 = T5EncoderModel.from_pretrained(bfl_repo, subfolder="text_encoder_2", torch_dtype=torch_dtype,)
    transformer = FluxTransformer2DModel.from_pretrained(bfl_repo, subfolder="transformer", torch_dtype=torch_dtype,)
    vae = AutoencoderKL.from_pretrained(bfl_repo, subfolder="vae")
    # transformer = FluxTransformer2DModel.from_single_file("Kijai/flux-fp8/flux1-dev-fp8.safetensors", torch_dtype=torch_dtype)
    pipe = FluxTryonPipeline.from_pretrained(
        bfl_repo,
        transformer=transformer,
        text_encoder=text_encoder,
        text_encoder_2=text_encoder_2,
        vae=vae,
        torch_dtype=torch_dtype,
    ).to(device="cpu", dtype=torch_dtype)
    # pipe.transformer = torch.compile(pipe.transformer, mode="reduce-overhead", fullgraph=True) # resolution may change, not use this
    # # quantize transformer cause severe degration
    # quantize(pipe.transformer, weights=qfloat8)
    # freeze(pipe.transformer)
    # quantize(pipe.text_encoder_2, weights=qfloat8)
    # freeze(pipe.text_encoder_2)    
    pipe.to(device=device)

    # Enable memory efficient attention and VAE optimization
    pipe.enable_attention_slicing()
    # pipe.enable_sequential_cpu_offload()
    # pipe.enable_model_cpu_offload()
    pipe.vae.enable_slicing()
    pipe.vae.enable_tiling()

    pipe.load_lora_weights(
        "loooooong/Any2anyTryon",
        weight_name="dev_lora_any2any_tryon.safetensors",
        adapter_name="tryon",
    )
    return pipe

pipe = load_models()

def crop_to_multiple_of_16(img):
    width, height = img.size
    
    # Calculate new dimensions that are multiples of 8
    new_width = width - (width % 16)  
    new_height = height - (height % 16)
    
    # Calculate crop box coordinates
    left = (width - new_width) // 2
    top = (height - new_height) // 2
    right = left + new_width
    bottom = top + new_height
    
    # Crop the image
    cropped_img = img.crop((left, top, right, bottom))
    
    return cropped_img

def resize_and_pad_to_size(image, target_width, target_height):
    # Convert numpy array to PIL Image if needed
    if isinstance(image, np.ndarray):
        image = Image.fromarray(image)
        
    # Get original dimensions
    orig_width, orig_height = image.size
    
    # Calculate aspect ratios
    target_ratio = target_width / target_height
    orig_ratio = orig_width / orig_height
    
    # Calculate new dimensions while maintaining aspect ratio
    if orig_ratio > target_ratio:
        # Image is wider than target ratio - scale by width
        new_width = target_width
        new_height = int(new_width / orig_ratio)
    else:
        # Image is taller than target ratio - scale by height
        new_height = target_height
        new_width = int(new_height * orig_ratio)
        
    # Resize image
    resized_image = image.resize((new_width, new_height))
    
    # Create white background image of target size
    padded_image = Image.new('RGB', (target_width, target_height), 'white')
    
    # Calculate padding to center the image
    left_padding = (target_width - new_width) // 2
    top_padding = (target_height - new_height) // 2
    
    # Paste resized image onto padded background
    padded_image.paste(resized_image, (left_padding, top_padding))
    
    return padded_image, left_padding, top_padding, target_width - new_width - left_padding, target_height - new_height - top_padding

def resize_by_height(image, height):
    if isinstance(image, np.ndarray):
        image = Image.fromarray(image)
    # image is a PIL image
    image = image.resize((int(image.width * height / image.height), height))
    return crop_to_multiple_of_16(image)

# @spaces.GPU()
@torch.no_grad
def generate_image(prompt, model_image, garment_image, height=512, width=384, seed=0, guidance_scale=3.5, show_type="follow model image", num_inference_steps=30):
    height, width = int(height), int(width)
    width = width - (width % 16)  
    height = height - (height % 16)

    concat_image_list = []
    has_model_image = model_image is not None
    has_garment_image = garment_image is not None
    if has_model_image:
        if has_garment_image:
            # if both model and garment image are provided, ensure model image and target image have the same size
            input_height, input_width = model_image.shape[:2]
            model_image, lp, tp, rp, bp = resize_and_pad_to_size(Image.fromarray(model_image), width, height)
        else:
            model_image = resize_by_height(model_image, height)
        # model_image = resize_and_pad_to_size(Image.fromarray(model_image), width, height)
        concat_image_list.append(model_image)
    if has_garment_image:
        # if has_model_image:
        #     garment_image = resize_and_pad_to_size(Image.fromarray(garment_image), width, height)
        # else:
        garment_image = resize_by_height(garment_image, height)
        concat_image_list.append(garment_image)
    concat_image_list.append(np.zeros((height, width, 3), dtype=np.uint8))

    image = np.concatenate([np.array(img) for img in concat_image_list], axis=1)
    image = Image.fromarray(image)
    
    mask = np.zeros_like(image)
    mask[:,-width:] = 255
    mask_image = Image.fromarray(mask)
    
    assert height==image.height, "ensure same height"
    # with torch.cuda.amp.autocast(): # this cause black image
    # with torch.no_grad():
    output = pipe(
        prompt,
        image=image,
        mask_image=mask_image,
        strength=1.,
        height=height,
        width=image.width,
        target_width=width,
        tryon=has_model_image and has_garment_image,
        guidance_scale=guidance_scale,
        num_inference_steps=num_inference_steps,
        max_sequence_length=512,
        generator=torch.Generator("cpu").manual_seed(seed),
        output_type="latent",
    ).images
    
    latents = pipe._unpack_latents(output, image.height, image.width, pipe.vae_scale_factor)
    if show_type!="all outputs":
        latents = latents[:,:,:,-width//pipe.vae_scale_factor:]
    latents = (latents / pipe.vae.config.scaling_factor) + pipe.vae.config.shift_factor
    image = pipe.vae.decode(latents, return_dict=False)[0]
    image = pipe.image_processor.postprocess(image, output_type="pil")[0]
    output = image
    if show_type=="follow model image" and has_model_image and has_garment_image:
        output = output.crop((lp, tp, output.width-rp, output.height-bp)).resize((input_width, input_height))
    
    return output

def update_dimensions(model_image, garment_image, height, width, auto_ar):
    if not auto_ar:
        return height, width
    if model_image is not None:
        height = model_image.shape[0]
        width = model_image.shape[1]
    elif garment_image is not None:
        height = garment_image.shape[0]
        width = garment_image.shape[1]
    else:
        height = 512
        width = 384

    # Set max dimensions and minimum size
    max_height = 1024
    max_width = 1024
    min_size = 384

    # Scale down if exceeds max dimensions while maintaining aspect ratio
    if height > max_height or width > max_width:
        aspect_ratio = width / height
        if height > max_height:
            height = max_height
            width = int(height * aspect_ratio)
        if width > max_width:
            width = max_width
            height = int(width / aspect_ratio)

    # Scale up if below minimum size while maintaining aspect ratio
    if height < min_size and width < min_size:
        aspect_ratio = width / height
        if height < width:
            height = min_size
            width = int(height * aspect_ratio)
        else:
            width = min_size
            height = int(width / aspect_ratio)

    return height, width

model1 = Image.open("asset/images/model/model1.png") 
model2 = Image.open("asset/images/model/model2.jpg")
model3 = Image.open("asset/images/model/model3.png") 
model4 = Image.open("asset/images/model/model4.png")

garment1 = Image.open("asset/images/garment/garment1.jpg") 
garment2 = Image.open("asset/images/garment/garment2.jpg")
garment3 = Image.open("asset/images/garment/garment3.jpg") 
garment4 = Image.open("asset/images/garment/garment4.jpg")

def launch_demo():
    with gr.Blocks() as demo:   
        gr.Markdown("# Any2AnyTryon")
        gr.Markdown("Demo(experimental) for [Any2AnyTryon: A Unified Framework for Virtual Try-on Generation](https://arxiv.org/abs/2501.15891) ([Code](https://github.com/logn-2024/Any2anyTryon)).") 
        with gr.Row():
            with gr.Column():
                model_image = gr.Image(label="Model Image", type="numpy", interactive=True,)
                with gr.Row():
                    garment_image = gr.Image(label="Garment Image", type="numpy", interactive=True,)
                    with gr.Column():
                        prompt = gr.Textbox(
                            label="Prompt",
                            info="Try example prompts from right side",
                            placeholder="Enter your prompt here...",
                            value="",
                            visible=False,
                        )
                        with gr.Row(visible=False):
                            height = gr.Number(label="Height", value=768, precision=0)
                            width = gr.Number(label="Width", value=576, precision=0)
                        seed = gr.Number(label="Seed", value=0, precision=0)
                        with gr.Accordion("Advanced Settings", open=False):
                            guidance_scale = gr.Number(label="Guidance Scale", value=3.5)
                            num_inference_steps = gr.Number(label="Inference Steps", value=15)
                            show_type = gr.Radio(label="Show Type",choices=["follow model image", "follow height & width", "all outputs"],value="follow model image")
                            auto_ar = gr.Checkbox(label="Detect Image Size(From Uploaded Images)", value=False, visible=False,)
                btn = gr.Button("Generate")
            
            with gr.Column():
                output = gr.Image(label="Generated Image")
                example_prompts = gr.Examples(
                        [
                            "<MODEL> a person with fashion garment. <GARMENT> a garment. <TARGET> model with fashion garment",
                            "<MODEL> a person with fashion garment. <TARGET> the same garment laid flat.",
                            "<GARMENT> The image shows a fashion garment. <TARGET> a smiling person with the garment in white background",
                        ],
                        inputs=prompt,
                        label="Example Prompts",
                        visible=False
                    )
                example_model = gr.Examples(
                    examples=[
                        model1, model2, model3, model4
                    ],
                    inputs=model_image,
                    label="Example Model Images"
                )
                example_garment = gr.Examples(
                    examples=[
                        garment1, garment2, garment3, garment4
                    ],
                    inputs=garment_image,
                    label="Example Garment Images"
                )

        # Update dimensions when images change
        model_image.change(fn=update_dimensions, 
                        inputs=[model_image, garment_image, height, width, auto_ar],
                        outputs=[height, width])
        garment_image.change(fn=update_dimensions,
                            inputs=[model_image, garment_image, height, width, auto_ar], 
                            outputs=[height, width])    
        btn.click(fn=generate_image,
                inputs=[prompt, model_image, garment_image, height, width, seed, guidance_scale, show_type, num_inference_steps],
                outputs=output)

        demo.title = "FLUX Image Generation Demo"
        demo.description = "Generate images using FLUX model with LoRA"
        
        examples = [
            # tryon
            [
                # '''''',
                model1,
                garment1,
                768, 576
            ],
            [
                # '''''',
                model2,
                garment2,
                768, 576
            ],
            [
                # '''''',
                model3,
                garment3,
                768, 576
            ],
            [
                # '''''',
                model4,
                garment4,
                768, 576
            ],
        ]
        
        gr.Examples(
            examples=examples,
            inputs=[model_image, garment_image], # prompt
            outputs=output,
            fn=generate_image,
            cache_examples=False,
            examples_per_page=20
        )
    demo.queue().launch(share=False, show_error=False,
        server_name="0.0.0.0"
    )
if __name__ == "__main__":
    launch_demo()
