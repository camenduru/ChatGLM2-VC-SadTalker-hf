import os, sys
import tempfile
import gradio as gr
from src.gradio_demo import SadTalker  
# from src.utils.text2speech import TTSTalker
from huggingface_hub import snapshot_download

import torch
import librosa
from scipy.io.wavfile import write
from transformers import WavLMModel

import utils
from models import SynthesizerTrn
from mel_processing import mel_spectrogram_torch
from speaker_encoder.voice_encoder import SpeakerEncoder

import time
from textwrap import dedent

import mdtex2html
from loguru import logger
from transformers import AutoModel, AutoTokenizer

from tts_voice import tts_order_voice
import edge_tts
import tempfile
import anyio
import asyncio


def get_source_image(image):   
        return image

try:
    import webui  # in webui
    in_webui = True
except:
    in_webui = False


def toggle_audio_file(choice):
    if choice == False:
        return gr.update(visible=True), gr.update(visible=False)
    else:
        return gr.update(visible=False), gr.update(visible=True)
    
def ref_video_fn(path_of_ref_video):
    if path_of_ref_video is not None:
        return gr.update(value=True)
    else:
        return gr.update(value=False)
    
def download_model():
    REPO_ID = 'vinthony/SadTalker-V002rc'
    snapshot_download(repo_id=REPO_ID, local_dir='./checkpoints', local_dir_use_symlinks=True)

def sadtalker_demo():

    download_model()

    sad_talker = SadTalker(lazy_load=True)
    # tts_talker = TTSTalker()

download_model()
sad_talker = SadTalker(lazy_load=True)


# ChatGLM2 & FreeVC

'''
def get_wavlm():
    os.system('gdown https://drive.google.com/uc?id=12-cB34qCTvByWT-QtOcZaqwwO21FLSqU')
    shutil.move('WavLM-Large.pt', 'wavlm')
'''

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

smodel = SpeakerEncoder('speaker_encoder/ckpt/pretrained_bak_5805000.pt')

print("Loading FreeVC(24k)...")
hps = utils.get_hparams_from_file("configs/freevc-24.json")
freevc_24 = SynthesizerTrn(
    hps.data.filter_length // 2 + 1,
    hps.train.segment_size // hps.data.hop_length,
    **hps.model).to(device)
_ = freevc_24.eval()
_ = utils.load_checkpoint("checkpoint/freevc-24.pth", freevc_24, None)

print("Loading WavLM for content...")
cmodel = WavLMModel.from_pretrained("microsoft/wavlm-large").to(device)
 
def convert(model, src, tgt):
    with torch.no_grad():
        # tgt
        wav_tgt, _ = librosa.load(tgt, sr=hps.data.sampling_rate)
        wav_tgt, _ = librosa.effects.trim(wav_tgt, top_db=20)
        if model == "FreeVC" or model == "FreeVC (24kHz)":
            g_tgt = smodel.embed_utterance(wav_tgt)
            g_tgt = torch.from_numpy(g_tgt).unsqueeze(0).to(device)
        else:
            wav_tgt = torch.from_numpy(wav_tgt).unsqueeze(0).to(device)
            mel_tgt = mel_spectrogram_torch(
                wav_tgt, 
                hps.data.filter_length,
                hps.data.n_mel_channels,
                hps.data.sampling_rate,
                hps.data.hop_length,
                hps.data.win_length,
                hps.data.mel_fmin,
                hps.data.mel_fmax
            )
        # src
        wav_src, _ = librosa.load(src, sr=hps.data.sampling_rate)
        wav_src = torch.from_numpy(wav_src).unsqueeze(0).to(device)
        c = cmodel(wav_src).last_hidden_state.transpose(1, 2).to(device)
        # infer
        if model == "FreeVC":
            audio = freevc.infer(c, g=g_tgt)
        elif model == "FreeVC-s":
            audio = freevc_s.infer(c, mel=mel_tgt)
        else:
            audio = freevc_24.infer(c, g=g_tgt)
        audio = audio[0][0].data.cpu().float().numpy()
        if model == "FreeVC" or model == "FreeVC-s":
            write("out.wav", hps.data.sampling_rate, audio)
        else:
            write("out.wav", 24000, audio)
    out = "out.wav"
    return out

# GLM2

#language_dict = tts_order_voice

tts_voice_list = asyncio.get_event_loop().run_until_complete(edge_tts.list_voices())
voices = [f"{v['ShortName']}-{v['Gender']}" for v in tts_voice_list]

# fix timezone in Linux
os.environ["TZ"] = "Asia/Shanghai"
try:
    time.tzset()  # type: ignore # pylint: disable=no-member
except Exception:
    # Windows
    logger.warning("Windows, cant run time.tzset()")

# model_name = "THUDM/chatglm2-6b"
model_name = "THUDM/chatglm2-6b-int4"

RETRY_FLAG = False

tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)

# model = AutoModel.from_pretrained(model_name, trust_remote_code=True).cuda()

# 4/8 bit
# model = AutoModel.from_pretrained("THUDM/chatglm2-6b", trust_remote_code=True).quantize(4).cuda()

has_cuda = torch.cuda.is_available()

# has_cuda = False  # force cpu

if has_cuda:
    model_glm = (
        AutoModel.from_pretrained(model_name, trust_remote_code=True).cuda().half()
    )  # 3.92G
else:
    model_glm = AutoModel.from_pretrained(
        model_name, trust_remote_code=True
    ).float()  # .float() .half().float()

model_glm = model_glm.eval()

_ = """Override Chatbot.postprocess"""


def postprocess(self, y):
    if y is None:
        return []
    for i, (message, response) in enumerate(y):
        y[i] = (
            None if message is None else mdtex2html.convert((message)),
            None if response is None else mdtex2html.convert(response),
        )
    return y


gr.Chatbot.postprocess = postprocess


def parse_text(text):
    """copy from https://github.com/GaiZhenbiao/ChuanhuChatGPT/"""
    lines = text.split("\n")
    lines = [line for line in lines if line != ""]
    count = 0
    for i, line in enumerate(lines):
        if "```" in line:
            count += 1
            items = line.split("`")
            if count % 2 == 1:
                lines[i] = f'<pre><code class="language-{items[-1]}">'
            else:
                lines[i] = "<br></code></pre>"
        else:
            if i > 0:
                if count % 2 == 1:
                    line = line.replace("`", r"\`")
                    line = line.replace("<", "&lt;")
                    line = line.replace(">", "&gt;")
                    line = line.replace(" ", "&nbsp;")
                    line = line.replace("*", "&ast;")
                    line = line.replace("_", "&lowbar;")
                    line = line.replace("-", "&#45;")
                    line = line.replace(".", "&#46;")
                    line = line.replace("!", "&#33;")
                    line = line.replace("(", "&#40;")
                    line = line.replace(")", "&#41;")
                    line = line.replace("$", "&#36;")
                lines[i] = "<br>" + line
    text = "".join(lines)
    return text


def predict(
    RETRY_FLAG, input, chatbot, max_length, top_p, temperature, history, past_key_values
):
    try:
        chatbot.append((parse_text(input), ""))
    except Exception as exc:
        logger.error(exc)
        logger.debug(f"{chatbot=}")
        _ = """
        if chatbot:
            chatbot[-1] = (parse_text(input), str(exc))
            yield chatbot, history, past_key_values
        # """
        yield chatbot, history, past_key_values

    for response, history, past_key_values in model_glm.stream_chat(
        tokenizer,
        input,
        history,
        past_key_values=past_key_values,
        return_past_key_values=True,
        max_length=max_length,
        top_p=top_p,
        temperature=temperature,
    ):
        chatbot[-1] = (parse_text(input), parse_text(response))
        # chatbot[-1][-1] = parse_text(response)

        yield chatbot, history, past_key_values, parse_text(response)


def trans_api(input, max_length=4096, top_p=0.8, temperature=0.2):
    if max_length < 10:
        max_length = 4096
    if top_p < 0.1 or top_p > 1:
        top_p = 0.85
    if temperature <= 0 or temperature > 1:
        temperature = 0.01
    try:
        res, _ = model_glm.chat(
            tokenizer,
            input,
            history=[],
            past_key_values=None,
            max_length=max_length,
            top_p=top_p,
            temperature=temperature,
        )
        # logger.debug(f"{res=} \n{_=}")
    except Exception as exc:
        logger.error(f"{exc=}")
        res = str(exc)

    return res


def reset_user_input():
    return gr.update(value="")


def reset_state():
    return [], [], None, ""


# Delete last turn
def delete_last_turn(chat, history):
    if chat and history:
        chat.pop(-1)
        history.pop(-1)
    return chat, history


# Regenerate response
def retry_last_answer(
    user_input, chatbot, max_length, top_p, temperature, history, past_key_values
):
    if chatbot and history:
        # Removing the previous conversation from chat
        chatbot.pop(-1)
        # Setting up a flag to capture a retry
        RETRY_FLAG = True
        # Getting last message from user
        user_input = history[-1][0]
        # Removing bot response from the history
        history.pop(-1)

    yield from predict(
        RETRY_FLAG,  # type: ignore
        user_input,
        chatbot,
        max_length,
        top_p,
        temperature,
        history,
        past_key_values,
    )

# print

def print(text):
    return text

# TTS

async def text_to_speech_edge(text, voice):

    communicate = edge_tts.Communicate(text, "-".join(voice.split('-')[:-1]))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
        tmp_path = tmp_file.name

    await communicate.save(tmp_path)

    return tmp_path


with gr.Blocks(title="ChatGLM2-6B-int4", theme=gr.themes.Soft(text_size="sm"), analytics_enabled=False) as demo:
    gr.HTML("<center>"
            "<h1>📺💕🎶 - ChatGLM2 + Voice Cloning + SadTalker</h1>"
            "</center>")
    gr.Markdown("## <center>🥳 - Chat with any character you like through ChatGLM2-6B, FreeVC and SadTalker in real time</center>")
    gr.Markdown("## <center>⭐ - If you like the this app, please star my [GitHub repo](https://github.com/KevinWang676/ChatGLM2-Voice-Cloning)</center>")

    with gr.Tab("🍻 - ChatGLM2+VC"):
        with gr.Accordion("📒 Info", open=False):
            _ = f""" Some parameters of ChatGLM2:
                * Low temperature: responses will be more deterministic and focused; High temperature: responses more creative.
                * Suggested temperatures -- translation: up to 0.3; chatting: > 0.4
                * Top P controls dynamic vocabulary selection based on context.\n
                If you would like to chat with a specific character, you can send a prompt like "please act as Elon Musk and chat with me" to ChatGLM2.
                
                """
            gr.Markdown(dedent(_))
        chatbot = gr.Chatbot(height=300)
        with gr.Row():
            with gr.Column(scale=4):
                with gr.Column(scale=12):
                    user_input = gr.Textbox(
                        label="Chat with ChatGLM2 here",
                        placeholder="Enter something here...",
                    )
                    RETRY_FLAG = gr.Checkbox(value=False, visible=False)
        with gr.Column(min_width=32, scale=1):
            with gr.Row():
                submitBtn = gr.Button("Chat now", variant="primary")
                deleteBtn = gr.Button("Delete last turn", variant="secondary")
                retryBtn = gr.Button("Regenerate", variant="secondary")
                    
        with gr.Accordion("🔧 Settings", open=False):
            with gr.Row():
                emptyBtn = gr.Button("Clear History")
                max_length = gr.Slider(
                    0,
                    32768,
                    value=8192,
                    step=1.0,
                    label="Maximum length",
                    interactive=True,
                )
                top_p = gr.Slider(
                    0, 1, value=0.85, step=0.01, label="Top P", interactive=True
                )
                temperature = gr.Slider(
                    0.01, 1, value=0.95, step=0.01, label="Temperature", interactive=True
                )
    
    
        with gr.Row():
            test1 = gr.Textbox(label="Response from ChatGLM2 (you can edit the content)", lines = 3)
            with gr.Column():
                language = gr.Dropdown(choices=voices, value="en-US-AnaNeural-Female", label="Please select a voice")
                tts_btn = gr.Button("Generate using Edge-TTS", variant="primary")
            output_audio = gr.Audio(type="filepath", label="Audio generated by Edge-TTS", interactive=False)
    
        tts_btn.click(text_to_speech_edge, inputs=[test1, language], outputs=[output_audio])
    
        with gr.Row():
            model_choice = gr.Dropdown(choices=["FreeVC", "FreeVC-s", "FreeVC (24kHz)"], value="FreeVC (24kHz)", label="Model", visible=False) 
            audio1 = output_audio
            audio2 = gr.Audio(label="Upload reference audio for voice cloning (~5s)", type='filepath')
            clone_btn = gr.Button("Generate using FreeVC", variant="primary")
            audio_cloned =  gr.Audio(label="Generated audio in a custom voice", type='filepath')
    
        clone_btn.click(convert, inputs=[model_choice, audio1, audio2], outputs=[audio_cloned])
            
        history = gr.State([])
        past_key_values = gr.State(None)
    
        user_input.submit(
            predict,
            [
                RETRY_FLAG,
                user_input,
                chatbot,
                max_length,
                top_p,
                temperature,
                history,
                past_key_values,
            ],
            [chatbot, history, past_key_values, test1],
            show_progress="full",
        )
        submitBtn.click(
            predict,
            [
                RETRY_FLAG,
                user_input,
                chatbot,
                max_length,
                top_p,
                temperature,
                history,
                past_key_values,
            ],
            [chatbot, history, past_key_values, test1],
            show_progress="full",
            api_name="predict",
        )
        submitBtn.click(reset_user_input, [], [user_input])
    
        emptyBtn.click(
            reset_state, outputs=[chatbot, history, past_key_values, test1], show_progress="full"
        )
    
        retryBtn.click(
            retry_last_answer,
            inputs=[
                user_input,
                chatbot,
                max_length,
                top_p,
                temperature,
                history,
                past_key_values,
            ],
            # outputs = [chatbot, history, last_user_message, user_message]
            outputs=[chatbot, history, past_key_values, test1],
        )
        deleteBtn.click(delete_last_turn, [chatbot, history], [chatbot, history])
    
        with gr.Accordion("📔 Prompts", open=False):
            etext = """In America, where cars are an important part of the national psyche, a decade ago people had suddenly started to drive less, which had not happened since the oil shocks of the 1970s. """
            examples = gr.Examples(
                examples=[
                    ["Explain the plot of Cinderella in a sentence."],
                    [
                        "How long does it take to become proficient in French, and what are the best methods for retaining information?"
                    ],
                    ["What are some common mistakes to avoid when writing code?"],
                    ["Build a prompt to generate a beautiful portrait of a horse"],
                    ["Suggest four metaphors to describe the benefits of AI"],
                    ["Write a pop song about leaving home for the sandy beaches."],
                    ["Write a summary demonstrating my ability to tame lions"],
                    ["鲁迅和周树人什么关系"],
                    ["从前有一头牛，这头牛后面有什么？"],
                    ["正无穷大加一大于正无穷大吗？"],
                    ["正无穷大加正无穷大大于正无穷大吗？"],
                    ["-2的平方根等于什么"],
                    ["树上有5只鸟，猎人开枪打死了一只。树上还有几只鸟？"],
                    ["树上有11只鸟，猎人开枪打死了一只。树上还有几只鸟？提示：需考虑鸟可能受惊吓飞走。"],
                    ["鲁迅和周树人什么关系 用英文回答"],
                    ["以红楼梦的行文风格写一张委婉的请假条。不少于320字。"],
                    [f"{etext} 翻成中文，列出3个版本"],
                    [f"{etext} \n 翻成中文，保留原意，但使用文学性的语言。不要写解释。列出3个版本"],
                    ["js 判断一个数是不是质数"],
                    ["js 实现python 的 range(10)"],
                    ["js 实现python 的 [*(range(10)]"],
                    ["假定 1 + 2 = 4, 试求 7 + 8"],
                    ["Erkläre die Handlung von Cinderella in einem Satz."],
                    ["Erkläre die Handlung von Cinderella in einem Satz. Auf Deutsch"],
                ],
                inputs=[user_input],
                examples_per_page=30,
            )
    
        with gr.Accordion("For Chat/Translation API", open=False, visible=False):
            input_text = gr.Text()
            tr_btn = gr.Button("Go", variant="primary")
            out_text = gr.Text()
        tr_btn.click(
            trans_api,
            [input_text, max_length, top_p, temperature],
            out_text,
            # show_progress="full",
            api_name="tr",
        )
        _ = """
        input_text.submit(
            trans_api,
            [input_text, max_length, top_p, temperature],
            out_text,
            show_progress="full",
            api_name="tr1",
        )
        # """

    with gr.Tab("📺 - SadTalker"):
        with gr.Row().style(equal_height=False):
            with gr.Column(variant='panel'):
                with gr.Tabs(elem_id="sadtalker_source_image"):
                    with gr.TabItem('Source image'):
                        with gr.Row():
                            source_image = gr.Image(label="Please upload an image here", source="upload", type="filepath", elem_id="img2img_image").style(width=512)
    
    
                with gr.Tabs(elem_id="sadtalker_driven_audio"):
                    with gr.TabItem('💡You can also download the generated video if you want'):
    
                        with gr.Row():
                            driven_audio = audio_cloned
                            driven_audio_no = gr.Audio(label="Use IDLE mode, no audio is required", source="upload", type="filepath", visible=False)
    
                            with gr.Column():
                                use_idle_mode = gr.Checkbox(label="Use Idle Animation", visible=False)
                                length_of_audio = gr.Number(value=5, label="The length(seconds) of the generated video.", visible=False)
                                use_idle_mode.change(toggle_audio_file, inputs=use_idle_mode, outputs=[driven_audio, driven_audio_no]) # todo
    
                        with gr.Row():
                            ref_video = gr.Video(label="Reference Video", source="upload", type="filepath", elem_id="vidref", visible=False).style(width=512)
    
                            with gr.Column():
                                use_ref_video = gr.Checkbox(label="Use Reference Video", visible=False)
                                ref_info = gr.Radio(['pose', 'blink','pose+blink', 'all'], value='pose', label='Reference Video',info="How to borrow from reference Video?((fully transfer, aka, video driving mode))", visible=False)
    
                            ref_video.change(ref_video_fn, inputs=ref_video, outputs=[use_ref_video]) # todo
    
    
            with gr.Column(variant='panel'):
                with gr.Tabs(elem_id="sadtalker_checkbox"):
                    with gr.TabItem('🔧 Settings'):
                        with gr.Column(variant='panel'):
                            # width = gr.Slider(minimum=64, elem_id="img2img_width", maximum=2048, step=8, label="Manually Crop Width", value=512) # img2img_width
                            # height = gr.Slider(minimum=64, elem_id="img2img_height", maximum=2048, step=8, label="Manually Crop Height", value=512) # img2img_width
                            with gr.Row():
                                pose_style = gr.Slider(minimum=0, maximum=45, step=1, label="Pose style", value=0, visible=False) #
                                exp_weight = gr.Slider(minimum=0, maximum=3, step=0.1, label="expression scale", value=1, visible=False) #
                                blink_every = gr.Checkbox(label="use eye blink", value=True, visible=False)
    
                            with gr.Row():
                                size_of_image = gr.Radio([256, 512], value=256, label='face model resolution', info="use 256/512 model?", visible=False) #
                                preprocess_type = gr.Radio(['crop', 'full'], value='crop', label='How to handle the input image?', info="crop: resize the image; full: not resize")
    
                            with gr.Row():
                                is_still_mode = gr.Checkbox(label="Still mode (fewer head motion)", value=True)
                                facerender = gr.Radio(['facevid2vid','pirender'], value='facevid2vid', label='facerender', info="which face render?", visible=False)
    
                            with gr.Row():
                                batch_size = gr.Slider(label="Batch size in generation", step=1, maximum=32, value=2)
                                enhancer = gr.Checkbox(label="GFPGAN as Face enhancer", value=True, visible=False)
    
                            submit = gr.Button('Start video chat now', elem_id="sadtalker_generate", variant='primary')
    
                with gr.Tabs(elem_id="sadtalker_genearted"):
                        gen_video = gr.Video(label="Generated video", format="mp4").style(width=256)
    
    
    
        submit.click(
                fn=sad_talker.test,
                inputs=[source_image,
                        driven_audio,
                        preprocess_type,
                        is_still_mode,
                        enhancer,
                        batch_size,
                        size_of_image,
                        pose_style,
                        facerender,
                        exp_weight,
                        use_ref_video,
                        ref_video,
                        ref_info,
                        use_idle_mode,
                        length_of_audio,
                        blink_every
                        ],
                outputs=[gen_video]
                )    
    gr.Markdown("### <center>❗ Please do not generate content that could infringe upon the rights or cause harm to individuals or organizations.</center>")
    gr.Markdown("<center>💡 - How to use this app：After sending your questions to ChatGLM2, click “Chat now”, “Generate using Edge-TTS”, “Generate using FreeVC” and “Start video chat now” in turn.</center>")

demo.queue().launch(show_error=True, debug=True)
