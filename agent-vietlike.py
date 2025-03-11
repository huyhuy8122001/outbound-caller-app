from __future__ import annotations

import asyncio
import logging
from dotenv import load_dotenv
import json
import os
from time import perf_counter
from typing import Annotated
from livekit import rtc, api
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.multimodal import MultimodalAgent
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, silero, elevenlabs, google
from livekit.plugins.elevenlabs import tts
import requests

# load environment variables, this is optional, only used for local development
load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
_default_instructions = (
    "Bạn là AI Agent Khảo Sát Chất Lượng Tour Du Lịch – Vietlike Travel. "
    "Nhiệm vụ của bạn là thu thập phản hồi từ khách hàng sau chuyến tour. "
    "Hãy giao tiếp thân thiện, lịch sự, chuyên nghiệp, đặt các câu hỏi ngắn gọn và dễ hiểu để tối ưu hóa thời gian của khách hàng. "
    "Bạn cần ghi nhận đầy đủ các phản hồi và lưu trữ dữ liệu một cách chính xác và bảo mật. "
    "Cụ thể, bạn cần thực hiện các bước sau:\n"
    "1. Mở đầu cuộc gọi với lời chào: \"Chào {address} {name}, em là [Tên AI Agent] từ Vietlike Travel. Cảm ơn {address} đã tham gia tour {tour_name} vừa rồi. Không biết {address} có thời gian chia sẻ cảm nhận về chuyến đi không ạ?\"\n"
    "2. Nếu khách đồng ý, lần lượt đặt các câu hỏi khảo sát:\n"
    "   - \"{address} cảm thấy hài lòng nhất điều gì trong chuyến đi vừa qua?\" (Điểm khách hàng hài lòng nhất)\n"
    "   - \"Trong quá trình đi tour, có điểm nào {address} cảm thấy chưa hài lòng hoặc cần cải thiện không ạ?\" (Điểm cần cải thiện)\n"
    "   - \"{address} đánh giá thế nào về thái độ và sự hỗ trợ của hướng dẫn viên/nhân viên? (trên thang điểm từ 1 đến 5)\"\n"
    "   - \"Nếu có dịp, {address} có mong muốn tham gia các tour khác của Vietlike Travel không? {address} có gợi ý thêm loại hình tour nào phù hợp không ạ?\"\n"
    "3. Cảm ơn khách hàng khách rằng: \"Cảm ơn {address} rất nhiều vì đã chia sẻ. Phản hồi của {address} sẽ giúp Vietlike Travel nâng cao chất lượng dịch vụ. Em sẽ kết thúc cuộc gọi ngay bây giờ. Chúc {address} một ngày tốt lành!\" "
    "và ngay sau đó gọi hàm end_call() để kết thúc cuộc gọi mà không cần chờ phản hồi từ khách.\n"
    "4. Cuối cùng, tổng hợp kết quả và gọi API webhook để lưu trữ phản hồi của khách hàng vào Google Sheet. Lưu ý: không thông báo đến khách hàng quá trình xử lý dữ liệu."
)

async def entrypoint(ctx: JobContext):
    global _default_instructions, outbound_trunk_id
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    user_identity = "phone_user"
    # the phone number to dial is provided in the job metadata
    metadata = json.loads(ctx.job.metadata)
    phone_number = metadata.get("phone_number")
    customer_name = metadata.get("customer_name")
    address = metadata.get("address")
    tour_name = metadata.get("tour_name")  # Thêm tour_name
    if not phone_number or not customer_name:
        logger.error("Phone number hoặc customer name không được cung cấp trong metadata.")
        return
    
    logger.info(f"Gọi đến {phone_number} cho khách hàng {customer_name}")

    # look up the user's phone number and appointment details
    
    instructions = (
        _default_instructions
        + f"Thông tin khách hàng: Tên: {customer_name}, Tour: {tour_name}, Xưng hô: {address}.\n"
        + "Lưu ý: không thông báo quy trình xử lý dữ liệu cho khách hàng."
    )

    # `create_sip_participant` starts dialing the user
    await ctx.api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=outbound_trunk_id,
            sip_call_to=phone_number,
            participant_identity=user_identity,
        )
    )

    # a participant is created as soon as we start dialing
    participant = await ctx.wait_for_participant(identity=user_identity)

    # start the agent, either a VoicePipelineAgent or MultimodalAgent
    # this can be started before the user picks up. The agent will only start
    # speaking once the user answers the call.
    run_voice_pipeline_agent(ctx, participant, instructions, phone_number, customer_name, address, tour_name)
    # run_multimodal_agent(ctx, participant, instructions)

    # in addition, you can monitor the call status separately
    start_time = perf_counter()
    while perf_counter() - start_time < 60:
        call_status = participant.attributes.get("sip.callStatus")
        # logger.info(call_status)
        # logger.info(call_status)
        # logger.info(participant.attributes)
        if call_status == "active":
            logger.info("user has picked up")
            return
        elif call_status == "automation":
            # if DTMF is used in the `sip_call_to` number, typically used to dial
            # an extension or enter a PIN.
            # during DTMF dialing, the participant will be in the "automation" state
            logger.info("automation")
            pass
        elif participant.disconnect_reason == rtc.DisconnectReason.USER_REJECTED:
            logger.info("user rejected the call, exiting job")
            break
        elif participant.disconnect_reason == rtc.DisconnectReason.USER_UNAVAILABLE:
            logger.info("user did not pick up, exiting job")
            break
        await asyncio.sleep(0.1)

    logger.info("session timed out, exiting job")
    ctx.shutdown()


class CallActions(llm.FunctionContext):
    """
    Detect user intent and perform actions
    """

    def __init__(
        self, *, api: api.LiveKitAPI, participant: rtc.RemoteParticipant, room: rtc.Room, phone_number: str
    ):
        super().__init__()

        self.api = api
        self.participant = participant
        self.room = room
        self.phone_number = phone_number

    async def hangup(self):
        try:
            # await asyncio.sleep(2)
            # await asyncio.sleep(0.1)
            await self.api.room.remove_participant(
                api.RoomParticipantIdentity(
                    room=self.room.name,
                    identity=self.participant.identity,
                )
            )
        except Exception as e:
            # it's possible that the user has already hung up, this error can be ignored
            logger.info(f"received error while ending call: {e}")

    @llm.ai_callable()
    async def end_call(self):
        """
        Sử dụng để kết thúc cuộc gọi.
        """
        logger.info(f"ending the call for {self.participant.identity}")
        # await asyncio.sleep(2)
        await self.hangup()

    @llm.ai_callable()
    async def send_webhook(
        self,
        satisfaction_point: Annotated[str, "Điểm khách hàng hài lòng nhất về tour"],
        improvement_point: Annotated[str, "Điểm cần cải thiện trong tour"],
        guide_rating: Annotated[int, "Đánh giá hướng dẫn viên/nhân viên (từ 1 đến 5)"],
        future_tour_request: Annotated[str, "Nhu cầu hoặc mong muốn về tour du lịch trong tương lai"]
    ):
        """
        Gửi thông tin khảo sát của khách hàng đến webhook server.
        Các trường dữ liệu gửi đi gồm:
        - Điểm khách hàng hài lòng nhất
        - Điểm cần cải thiện
        - Đánh giá hướng dẫn viên/nhân viên (1-5)
        - Nhu cầu tour trong tương lai
        """
        url = "https://hook.eu2.make.com/afjk6pyu3s9ul4ejhecqp1com8eaajwr"
        data = {
            "phone_number": self.phone_number,
            "satisfaction_point": satisfaction_point,
            "improvement_point": improvement_point,
            "guide_rating": guide_rating,
            "future_tour_request": future_tour_request
        }
        try:
            response = requests.post(url, json=data)
            response.raise_for_status()
            logger.info("Đã gửi dữ liệu khảo sát đến webhook server thành công")
        except requests.exceptions.RequestException as e:
            logger.error(f"Lỗi khi gửi dữ liệu đến webhook server: {e}")


    # @llm.ai_callable()
    # async def look_up_availability(
    #     self,
    #     date: Annotated[str, "The date of the appointment to check availability for"],
    # ):
    #     """Called when the user asks about alternative appointment availability"""
    #     logger.info(
    #         f"looking up availability for {self.participant.identity} on {date}"
    #     )
    #     await asyncio.sleep(3)
    #     return json.dumps(
    #         {
    #             "available_times": ["1pm", "2pm", "3pm"],
    #         }
    #     )

    # @llm.ai_callable()
    # async def confirm_appointment(
    #     self,
    #     date: Annotated[str, "date of the appointment"],
    #     time: Annotated[str, "time of the appointment"],
    # ):
    #     """Called when the user confirms their appointment on a specific date. Use this tool only when they are certain about the date and time."""
    #     logger.info(
    #         f"confirming appointment for {self.participant.identity} on {date} at {time}"
    #     )
    #     return "reservation confirmed"

    @llm.ai_callable()
    async def detected_answering_machine(self):
        """Called when the call reaches voicemail. Use this tool AFTER you hear the voicemail greeting"""
        logger.info(f"detected answering machine for {self.participant.identity}")
        await self.hangup()


def run_voice_pipeline_agent(
    ctx: JobContext, participant: rtc.RemoteParticipant, instructions: str, phone_number: str, customer_name: str, address: str, tour_name: str
):
    logger.info("starting voice pipeline agent")

    initial_ctx = llm.ChatContext().append(
        role="system",
        text=instructions,
    )

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        ################# Deepram #####################
        # stt=deepgram.STT(model="nova-2-phonecall"),
        stt=deepgram.stt.STT(
            model="nova-2",
            # model="whisper-medium",
            interim_results=True,
            smart_format=True,
            punctuate=True,
            filler_words=True,
            profanity_filter=False,
            # keywords=[("LiveKit", 1.5)],
            language="vi",
        ),
        ##############################################
        
        ################# Google #####################
        # stt = google.STT(
        #     model="default",
        #     spoken_punctuation=True,
        #     languages="vi-vn"
        # ),
        ##############################################
        
        
        
        
        # llm=openai.LLM.with_vertex(model="google/gemini-2.0-flash-exp"),
        # llm=google.LLM(
        #     model="gemini-2.0-flash-exp",
        #     temperature="0.6",
        # ),
        llm=openai.LLM(model="gpt-4o"),
        # tts=openai.TTS(),
        
        # tts=google.TTS(
        #     gender="female",
        #     voice_name="vi-VN-Neural2-A",
        #     language="vi-vn"
        # ),
        
        tts=elevenlabs.tts.TTS(
            model="eleven_turbo_v2_5",
            voice=elevenlabs.tts.Voice(
            id="mvYbQ2cRw9pAg9c9WOAc",
            name="Vietlike",
            # id="bWvs6tS24bngxwBo8QJy",
            # name="Tố Uyên",
            #### noted
            # id="HAAKLJlaJeGl18MKHYeg",
            # name="Trang",
            category="premade",
            settings=elevenlabs.tts.VoiceSettings(
                stability=0.71,
                similarity_boost=0.5,
                style=0.0,
                use_speaker_boost=True
                ),
            ),
            language="vi",
            # streaming_latency=3,
            # enable_ssml_parsing=False,
            # chunk_length_schedule=[80, 120, 200, 260],
        ),
        chat_ctx=initial_ctx,
        fnc_ctx=CallActions(api=ctx.api, participant=participant, room=ctx.room, phone_number=phone_number),
    )

    agent.start(ctx.room, participant)
    
    asyncio.create_task(
        agent.say(
            f"Chào {address} {customer_name}, em là Uyên từ Vietlike Travel. Cảm ơn {address} đã tham gia tour {tour_name} vừa rồi. "
            f"{address} có thể dành chút thời gian để chia sẻ cảm nhận về chuyến đi không ạ?",
            allow_interruptions=False
        )
    )



def run_multimodal_agent(
    ctx: JobContext, participant: rtc.RemoteParticipant, instructions: str
):
    logger.info("starting multimodal agent")

    # model = openai.realtime.RealtimeModel(
    #     instructions=instructions,
    #     modalities=["audio", "text"],
    # )
    model=google.beta.realtime.RealtimeModel(
        voice="Puck",
        temperature=0.8,
        instructions=instructions,
        modalities=["audio", "text"]
    ),
    agent = MultimodalAgent(
        model=model,
        fnc_ctx=CallActions(api=ctx.api, participant=participant, room=ctx.room),
    )
    agent.start(ctx.room, participant)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    if not outbound_trunk_id or not outbound_trunk_id.startswith("ST_"):
        raise ValueError(
            "SIP_OUTBOUND_TRUNK_ID is not set. Please follow the guide at https://docs.livekit.io/agents/quickstarts/outbound-calls/ to set it up."
        )
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # giving this agent a name will allow us to dispatch it via API
            # automatic dispatch is disabled when `agent_name` is set
            agent_name="outbound-caller",
            # prewarm by loading the VAD model, needed only for VoicePipelineAgent
            prewarm_fnc=prewarm,
        )
    )
