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


# load environment variables, this is optional, only used for local development
load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
_default_instructions = (
    "Bạn là chuyên viên thu hồi công nợ của công ty Di Mô, tên là Hiền. Giao tiếp của bạn với khách hàng sẽ là giọng nói."
    "Bạn sẽ tham gia cuộc gọi với một khách hàng có khoản nợ chưa thanh toán. Nhiệm vụ của bạn là xác nhận danh tính khách, thông báo khoản nợ và đề nghị khách thanh toán. "
    "Cụ thể, bạn cần thực hiện các bước sau: "
    "1. Chào hỏi khách hàng và xác nhận đúng danh tính theo tên. Xin phép dành ít phút trao đổi "
    "2. Thông báo khoản nợ là xxx triệu đồng đã quá hạn thanh toán và yêu cầu khách thanh toán trước ngày 21 tháng 2 năm 2025. "
    "3. Hỏi khách hàng liệu có thể thanh toán đúng hạn không. "
    "   - Nếu khách có thể thanh toán đúng hạn, hãy xác nhận lại với khách. "
    "   - Nếu khách chưa thể thanh toán đúng hạn, hãy hỏi xem khách có thể thanh toán vào ngày nào. "
    "   - Nếu khách không muốn thanh toán, hãy ghi nhận và kết thúc cuộc gọi một cách lịch sự. "
    "Luôn giữ thái độ lịch sự, chuyên nghiệp và tập trung vào việc thu hồi nợ hiệu quả. "
    "Thông báo lịch sự đến khách hàng và kết thúc cuộc gọi ngay sau đó"
)
# _default_instructions = (
#     "Bạn là chuyên viên thu hồi công nợ của công ty Di Mô. Giao tiếp của bạn với khách hàng sẽ là giọng nói."
#     "Bạn sẽ tham gia cuộc gọi với một khách hàng có khoản nợ chưa thanh toán. Nhiệm vụ của bạn là xác nhận danh tính khách, thông báo khoản nợ và đề nghị khách thanh toán."
#     "Cụ thể, bạn cần thực hiện các bước sau:"
#     "1. Chào hỏi khách hàng và xác nhận đúng danh tính theo tên."
#     "2. Thông báo khoản nợ là xxx VND đã quá hạn thanh toán và yêu cầu khách thanh toán trước ngày DD/MM/YYYY."
#     "3. Hỏi khách hàng liệu có thể thanh toán đúng hạn không."
#     "   - Nếu khách có thể thanh toán đúng hạn, hãy xác nhận lại với khách."
#     "   - Nếu khách chưa thể thanh toán đúng hạn, hãy hỏi xem khách có thể thanh toán vào ngày nào."
#     "   - Nếu khách không muốn thanh toán, hãy ghi nhận và kết thúc cuộc gọi một cách lịch sự."
#     "Luôn giữ thái độ lịch sự, chuyên nghiệp và tập trung vào việc thu hồi nợ hiệu quả."
# )

async def entrypoint(ctx: JobContext):
    global _default_instructions, outbound_trunk_id
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    user_identity = "phone_user"
    # the phone number to dial is provided in the job metadata
    phone_number = ctx.job.metadata
    logger.info(f"dialing {phone_number} to room {ctx.room.name}")

    # look up the user's phone number and appointment details
    customer_name = "Nguyễn Tiến Dũng"
    debt_amount = "3 triệu 2 trăm ngàn đồng"
    due_date = "ngày 25 tháng 2 năm 2025."
    
    instructions = (
        _default_instructions
        + f"Tên khách hàng là {customer_name}. Khoản nợ cần thanh toán là {debt_amount} và đã quá hạn thanh toán. "
        + f"Vui lòng nhắc khách thanh toán trước ngày {due_date}. Nếu khách chưa thể thanh toán đúng hạn, hãy hỏi xem khách có thể thanh toán vào ngày nào. "
        + f"Trường hợp khách từ chối thanh toán, ghi nhận lại lý do và kết thúc cuộc gọi. "
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
    run_voice_pipeline_agent(ctx, participant, instructions)
    # run_multimodal_agent(ctx, participant, instructions)

    # in addition, you can monitor the call status separately
    start_time = perf_counter()
    while perf_counter() - start_time < 30:
        call_status = participant.attributes.get("sip.callStatus")
        if call_status == "active":
            logger.info("user has picked up")
            return
        elif call_status == "automation":
            # if DTMF is used in the `sip_call_to` number, typically used to dial
            # an extension or enter a PIN.
            # during DTMF dialing, the participant will be in the "automation" state
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
        self, *, api: api.LiveKitAPI, participant: rtc.RemoteParticipant, room: rtc.Room
    ):
        super().__init__()

        self.api = api
        self.participant = participant
        self.room = room

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
        """kết thúc cuộc gọi"""
        logger.info(f"ending the call for {self.participant.identity}")
        # await asyncio.sleep(2)
        await self.hangup()
    

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
    ctx: JobContext, participant: rtc.RemoteParticipant, instructions: str
):
    logger.info("starting voice pipeline agent")

    initial_ctx = llm.ChatContext().append(
        role="system",
        text=instructions,
    )

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        # stt=deepgram.STT(model="nova-2-phonecall"),
        stt=deepgram.stt.STT(
            model="nova-2-general",
   #         model="whisper-medium",
            interim_results=True,
            smart_format=True,
            punctuate=True,
            filler_words=True,
            profanity_filter=False,
            keywords=[("LiveKit", 1.5)],
            language="vi",
        ),
        # llm=openai.LLM.with_vertex(model="google/gemini-2.0-flash-exp"),
        # llm=google.LLM(
        #     model="gemini-2.0-flash-exp",
        #     temperature="0.6",
        # ),
        llm=openai.LLM(model="gpt-4o"),
        # tts=openai.TTS(),
        tts=elevenlabs.tts.TTS(
            model="eleven_turbo_v2_5",
            voice=elevenlabs.tts.Voice(
            # id="EXAVITQu4vr4xnSDxMaL",
            # name="Bella",
            id="HAAKLJlaJeGl18MKHYeg",
            name="Trang",
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
        fnc_ctx=CallActions(api=ctx.api, participant=participant, room=ctx.room),
    )

    agent.start(ctx.room, participant)
    
    asyncio.create_task(agent.say("Xin chào, tôi là Hiền từ công ty Di Mô. Tôi gọi để trao đổi về khoản nợ của bạn. Bạn có thể dành ít phút để trao đổi không?"))



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
