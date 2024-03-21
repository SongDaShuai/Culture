# import sys
# sys.path.append('./')

# pull request

from typing import Tuple

import os
import cv2
import math
import torch
import random
import numpy as np
import argparse

import PIL
from PIL import Image

import diffusers
from diffusers.utils import load_image
from diffusers.models import ControlNetModel
from diffusers import LCMScheduler

from huggingface_hub import hf_hub_download

import insightface
from insightface.app import FaceAnalysis

from gradio_demo.style_template import styles
from pipeline_stable_diffusion_xl_instantid_full import StableDiffusionXLInstantIDPipeline
from gradio_demo.model_util import load_models_xl, get_torch_device, torch_gc

import gradio as gr

from diffusers import StableDiffusionControlNetPipeline, ControlNetModel, UniPCMultistepScheduler,StableDiffusionControlNetImg2ImgPipeline
from controlnet_aux import OpenposeDetector

import sys
sys.path.append(r'/mnt/local2T/haodong/project/InstantID-faceswap-main')

import faceswap2



# global variable
MAX_SEED = np.iinfo(np.int32).max
device = get_torch_device()
dtype = torch.float16 if str(device).__contains__("cuda") else torch.float32
STYLE_NAMES = list(styles.keys())
DEFAULT_STYLE_NAME = "Watercolor"

# Load face encoder
app = FaceAnalysis(name='antelopev2', root='./', providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
app.prepare(ctx_id=0, det_size=(640, 640))

# Path to InstantID models
face_adapter = f'./checkpoints/ip-adapter.bin'
controlnet_path = f'./checkpoints/ControlNetModel'

# Load pipeline
controlnet = ControlNetModel.from_pretrained(controlnet_path, torch_dtype=dtype)

def main(pretrained_model_name_or_path="../IP-Adapter-main/stable-diffusion-v1-5", enable_lcm_arg=False):

    
    # load and disable LCM
    # pipe.load_lora_weights("latent-consistency/lcm-lora-sdxl")
    # pipe.disable_lora()

    def prepare_average_embeding(face_list):
        face_emebdings = []
        for face_path in face_list:
            face_image = load_image(face_path)
            face_image = resize_img(face_image)
            face_info = app.get(cv2.cvtColor(np.array(face_image), cv2.COLOR_RGB2BGR))
            face_info = sorted(face_info, key=lambda x:(x['bbox'][2]-x['bbox'][0])*x['bbox'][3]-x['bbox'][1])[-1] # only use the maximum face
            face_emb = face_info['embedding']
            face_emebdings.append(face_emb)

        return np.concatenate(face_emebdings)

    def prepareMaskAndPoseAndControlImage(pose_image, face_info, padding = 50, mask_grow = 20, resize = True):
        if padding < mask_grow:
            raise ValueError('mask_grow cannot be greater than padding')

        kps = face_info['kps']
        width, height = pose_image.size

        x1, y1, x2, y2 = face_info['bbox']
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)

        # check if image can contain padding & mask
        m_x1 = max(0, x1 - mask_grow)
        m_y1 = max(0, y1 - mask_grow)
        m_x2 = min(width, x2 + mask_grow)
        m_y2 = min(height, y2 + mask_grow)

        m_x1, m_y1, m_x2, m_y2 = int(m_x1), int(m_y1), int(m_x2), int(m_y2)

        p_x1 = max(0, x1 - padding)
        p_y1 = max(0, y1 - padding)
        p_x2 = min(width, x2 + padding)
        p_y2 = min(height,y2 + padding)

        p_x1, p_y1, p_x2, p_y2 = int(p_x1), int(p_y1), int(p_x2), int(p_y2)

        # mask
        mask = np.zeros([height, width, 3])
        mask[m_y1:m_y2, m_x1:m_x2] = 255
        mask = mask[p_y1:p_y2, p_x1:p_x2]
        mask = Image.fromarray(mask.astype(np.uint8))

        image = np.array(pose_image)[p_y1:p_y2, p_x1:p_x2]
        image = Image.fromarray(image.astype(np.uint8))

        # resize image and KPS
        original_width, original_height = image.size
        kps -= [p_x1, p_y1]
        if resize:
            mask = resize_img(mask)
            image = resize_img(image)
            new_width, new_height = image.size
            kps *= [new_width / original_width, new_height / original_height]
        control_image = draw_kps(image, kps)

        # (mask, pose, control PIL images), (original positon face + padding: x, y, w, h)
        return (mask, image, control_image), (p_x1, p_y1, original_width, original_height)



    def toggle_lcm_ui(value):
        if value:
            return (
                gr.update(minimum=0, maximum=100, step=1, value=5),
                gr.update(minimum=0.1, maximum=20.0, step=0.1, value=1.5)
            )
        else:
            return (
                gr.update(minimum=5, maximum=100, step=1, value=30),
                gr.update(minimum=0.1, maximum=20.0, step=0.1, value=5)
            )
    
    def randomize_seed_fn(seed: int, randomize_seed: bool) -> int:
        if randomize_seed:
            seed = random.randint(0, MAX_SEED)
        return seed
    
    def remove_tips():
        return gr.update(visible=False)

    def get_example():
        case = [
            [
                './examples/yann-lecun_resize.jpg',
                "a man",
                "Snow",
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, photo, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
            ],
            [
                './examples/musk_resize.jpeg',
                "a man",
                "Mars",
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, photo, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
            ],
            [
                './examples/sam_resize.png',
                "a man",
                "Jungle",
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, photo, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, gree",
            ],
            [
                './examples/schmidhuber_resize.png',
                "a man",
                "Neon",
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, photo, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
            ],
            [
                './examples/kaifu_resize.png',
                "a man",
                "Vibrant Color",
                "(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, photo, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
            ],
        ]
        return case

    def run_for_examples(face_file, prompt, style, negative_prompt):
        return generate_image(face_file, None, prompt, negative_prompt, style, 30, 0.8, 0.8, 5, 42, False, True)

    def convert_from_cv2_to_image(img: np.ndarray) -> Image:
        return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

    def convert_from_image_to_cv2(img: Image) -> np.ndarray:
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    def draw_kps(image_pil, kps, color_list=[(255,0,0), (0,255,0), (0,0,255), (255,255,0), (255,0,255)]):
        stickwidth = 4
        limbSeq = np.array([[0, 2], [1, 2], [3, 2], [4, 2]])
        kps = np.array(kps)

        w, h = image_pil.size
        out_img = np.zeros([h, w, 3])

        for i in range(len(limbSeq)):
            index = limbSeq[i]
            color = color_list[index[0]]

            x = kps[index][:, 0]
            y = kps[index][:, 1]
            length = ((x[0] - x[1]) ** 2 + (y[0] - y[1]) ** 2) ** 0.5
            angle = math.degrees(math.atan2(y[0] - y[1], x[0] - x[1]))
            polygon = cv2.ellipse2Poly((int(np.mean(x)), int(np.mean(y))), (int(length / 2), stickwidth), int(angle), 0, 360, 1)
            out_img = cv2.fillConvexPoly(out_img.copy(), polygon, color)
        out_img = (out_img * 0.6).astype(np.uint8)

        for idx_kp, kp in enumerate(kps):
            color = color_list[idx_kp]
            x, y = kp
            out_img = cv2.circle(out_img.copy(), (int(x), int(y)), 10, color, -1)

        out_img_pil = Image.fromarray(out_img.astype(np.uint8))
        return out_img_pil

    def resize_img(input_image, max_side=1280, min_side=1024, size=None, 
                pad_to_max_side=False, mode=PIL.Image.BILINEAR, base_pixel_number=64):

            w, h = input_image.size
            if size is not None:
                w_resize_new, h_resize_new = size
            else:
                ratio = min_side / min(h, w)
                w, h = round(ratio*w), round(ratio*h)
                ratio = max_side / max(h, w)
                input_image = input_image.resize([round(ratio*w), round(ratio*h)], mode)
                w_resize_new = (round(ratio * w) // base_pixel_number) * base_pixel_number
                h_resize_new = (round(ratio * h) // base_pixel_number) * base_pixel_number
            input_image = input_image.resize([w_resize_new, h_resize_new], mode)

            if pad_to_max_side:
                res = np.ones([max_side, max_side, 3], dtype=np.uint8) * 255
                offset_x = (max_side - w_resize_new) // 2
                offset_y = (max_side - h_resize_new) // 2
                res[offset_y:offset_y+h_resize_new, offset_x:offset_x+w_resize_new] = np.array(input_image)
                input_image = Image.fromarray(res)
            return input_image

    def apply_style(style_name: str, positive: str, negative: str = "") -> Tuple[str, str]:
        p, n = styles.get(style_name, styles[DEFAULT_STYLE_NAME])
        return p.replace("{prompt}", positive), n + ' ' + negative
    
    

    def generate_image(face_image_path, pose_image_path, prompt, negative_prompt, style_name, num_steps, identitynet_strength_ratio, adapter_strength_ratio, guidance_scale, seed, enable_LCM, enhance_face_region, progress=gr.Progress(track_tqdm=True)):
        pretrained_path = "../IP-Adapter-main/stable-diffusion-v1-5"
        openpose_path = "./openpose"
        controlnet_path = "./sd-controlnet-openpose"
        lora_path = "../XunPu/Xunpu_V6.safetensors"
        pose_input = Image.open(pose_image_path).convert("RGB")

        openpose = OpenposeDetector.from_pretrained(openpose_path)
        # controlnet = [
        #     ControlNetModel.from_pretrained(controlnet_path, torch_dtype=torch.float16),
        # ]
        controlnet = ControlNetModel.from_pretrained(controlnet_path, torch_dtype=torch.float16)

        pipe = StableDiffusionControlNetPipeline.from_pretrained(
            pretrained_model_name_or_path=pretrained_path,
            controlnet=controlnet,
            torch_dtype=torch.float16,
        ).to("cuda")

        # pipe.load_lora_weights("/mnt/data_4t/yuxin/diffusers/lora/models/gugong/gugong-dian.safetensors")

        # pipe.scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config)
        # pipe.enable_xformers_memory_efficient_attention()
        # pipe.enable_model_cpu_offload()
        openpose_image = openpose(pose_input)
        images = [openpose_image]

        # lora
        pipe.load_lora_weights(lora_path)
        
        prompt = "a Chineses woman,Xunpu,hair flower,(8k, best quality, masterpiece, ultra highres:1.2)"
        negative_prompt = "(worst quality:2),(low quality:2),(normal quality:2),lowres,watermark,badhandv4,ng_deepnegative_v1_75t"
        img2img_images = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image=openpose_image,
            num_inference_steps=20,
            controlnet_conditioning_scale=1.0,
            num_images_per_prompt=1,
        ).images

        img2img_image = img2img_images[0]

        img2img_image.save("output.jpg","JPEG")

        print(type(face_image_path))
        #result=faceswap2.func("yangmi.jpg","output.jpg")
        result=faceswap2.func(face_image_path,"output.jpg")


        return result, gr.update(visible=True)

    ### Description
    title = r"""
    <h1 align="center">InstantID: Zero-shot Identity-Preserving Generation in Seconds</h1>
    """

    description = r"""
    <b>Official 🤗 Gradio demo</b> for <a href='https://github.com/InstantID/InstantID' target='_blank'><b>InstantID: Zero-shot Identity-Preserving Generation in Seconds</b></a>.<br>

    How to use:<br>
    1. Upload an image with a face. For images with multiple faces, we will only detect the largest face. Ensure the face is not too small and is clearly visible without significant obstructions or blurring.
    2. (Optional) You can upload another image as a reference for the face pose. If you don't, we will use the first detected face image to extract facial landmarks. If you use a cropped face at step 1, it is recommended to upload it to define a new face pose.
    3. Enter a text prompt, as done in normal text-to-image models.
    4. Click the <b>Submit</b> button to begin customization.
    5. Share your customized photo with your friends and enjoy! 😊
    """

    article = r"""
    ---
    📝 **Citation**
    <br>
    If our work is helpful for your research or applications, please cite us via:
    ```bibtex
    @article{wang2024instantid,
    title={InstantID: Zero-shot Identity-Preserving Generation in Seconds},
    author={Wang, Qixun and Bai, Xu and Wang, Haofan and Qin, Zekui and Chen, Anthony},
    journal={arXiv preprint arXiv:2401.07519},
    year={2024}
    }
    ```
    📧 **Contact**
    <br>
    If you have any questions, please feel free to open an issue or directly reach us out at <b>haofanwang.ai@gmail.com</b>.
    """

    tips = r"""
    ### Usage tips of InstantID
    1. If you're not satisfied with the similarity, try increasing the weight of "IdentityNet Strength" and "Adapter Strength."    
    2. If you feel that the saturation is too high, first decrease the Adapter strength. If it remains too high, then decrease the IdentityNet strength.
    3. If you find that text control is not as expected, decrease Adapter strength.
    4. If you find that realistic style is not good enough, go for our Github repo and use a more realistic base model.
    """

    css = '''
    .gradio-container {width: 85% !important}
    '''
    with gr.Blocks(css=css) as demo:

        # description
        gr.Markdown(title)
        gr.Markdown(description)

        with gr.Row():
            with gr.Column():
                
                # upload face image
                face_file = gr.Image(label="Upload a photo of your face", type="filepath")

                # optional: upload a reference pose image
                pose_file = gr.Image(label="Upload a reference pose image (optional)", type="filepath")
           
                # prompt
                prompt = gr.Textbox(label="Prompt",
                        info="Give simple prompt is enough to achieve good face fidelity",
                        placeholder="A photo of a person",
                        value="")
                
                submit = gr.Button("Submit", variant="primary")
                
                enable_LCM = gr.Checkbox(
                    label="Enable Fast Inference with LCM", value=enable_lcm_arg,
                    info="LCM speeds up the inference step, the trade-off is the quality of the generated image. It performs better with portrait face images rather than distant faces",
                )
                style = gr.Dropdown(label="Style template", choices=STYLE_NAMES, value=DEFAULT_STYLE_NAME)
                
                # strength
                identitynet_strength_ratio = gr.Slider(
                    label="IdentityNet strength (for fidelity)",
                    minimum=0,
                    maximum=1.5,
                    step=0.05,
                    value=0.80,
                )
                adapter_strength_ratio = gr.Slider(
                    label="Image adapter strength (for detail)",
                    minimum=0,
                    maximum=1.5,
                    step=0.05,
                    value=0.80,
                )
                
                with gr.Accordion(open=False, label="Advanced Options"):
                    negative_prompt = gr.Textbox(
                        label="Negative Prompt", 
                        placeholder="low quality",
                        value="(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
                    )
                    num_steps = gr.Slider( 
                        label="Number of sample steps",
                        minimum=20,
                        maximum=100,
                        step=1,
                        value=5 if enable_lcm_arg else 30,
                    )
                    guidance_scale = gr.Slider(
                        label="Guidance scale",
                        minimum=0.1,
                        maximum=10.0,
                        step=0.1,
                        value=0 if enable_lcm_arg else 5,
                    )
                    seed = gr.Slider(
                        label="Seed",
                        minimum=0,
                        maximum=MAX_SEED,
                        step=1,
                        value=42,
                    )
                    randomize_seed = gr.Checkbox(label="Randomize seed", value=True)
                    enhance_face_region = gr.Checkbox(label="Enhance non-face region", value=True)

            with gr.Column():
                gallery = gr.Image(label="Generated Images")
                usage_tips = gr.Markdown(label="Usage tips of InstantID", value=tips ,visible=False)

            submit.click(
                fn=remove_tips,
                outputs=usage_tips,            
            ).then(
                fn=randomize_seed_fn,
                inputs=[seed, randomize_seed],
                outputs=seed,
                queue=False,
                api_name=False,
            ).then(
                fn=generate_image,
                inputs=[face_file, pose_file, prompt, negative_prompt, style, num_steps, identitynet_strength_ratio, adapter_strength_ratio, guidance_scale, seed, enable_LCM, enhance_face_region],
                outputs=[gallery, usage_tips]
            )
        
            enable_LCM.input(fn=toggle_lcm_ui, inputs=[enable_LCM], outputs=[num_steps, guidance_scale], queue=False)

        gr.Examples(
            examples=get_example(),
            inputs=[face_file, prompt, style, negative_prompt],
            run_on_click=True,
            fn=run_for_examples,
            outputs=[gallery, usage_tips],
            cache_examples=True,
        )
        
        gr.Markdown(article)

    demo.launch()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretrained_model_name_or_path", type=str, default="wangqixun/YamerMIX_v8")
    parser.add_argument("--enable_LCM", type=bool, default=os.environ.get("ENABLE_LCM", False))

    args = parser.parse_args()

    main(args.pretrained_model_name_or_path, args.enable_LCM)