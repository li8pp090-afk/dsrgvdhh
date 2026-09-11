from aiogram import F, Router
from aiogram.enums import (
    ButtonStyle,
    ChatMemberStatus,
    ChatType,
)
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import get_mode, set_mode
from utils import chat_key


router = Router()


def mode_keyboard(mode):
    if mode == "voice":
        voice_style = ButtonStyle.PRIMARY
        default_style = ButtonStyle.DANGER
    else:
        voice_style = ButtonStyle.DANGER
        default_style = ButtonStyle.PRIMARY

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="فويس",
                    callback_data="mode:voice",
                    style=voice_style,
                ),
                InlineKeyboardButton(
                    text="افتراضي",
                    callback_data="mode:default",
                    style=default_style,
                ),
            ]
        ]
    )


@router.message(F.text == "ادت")
async def settings_handler(
    message: Message,
):
    if message.chat.type not in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        return

    if not message.from_user:
        return

    member = await message.bot.get_chat_member(
        message.chat.id,
        message.from_user.id,
    )

    if member.status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        return

    current_mode = get_mode(
        chat_key(message)
    )

    await message.answer(
        "تستطيع تغيير وضع عمل البوت\nمن هنا",
        reply_markup=mode_keyboard(
            current_mode
        ),
        reply_parameters=message.as_reply_parameters(),
    )


@router.callback_query(
    F.data.startswith("mode:")
)
async def mode_callback(
    callback: CallbackQuery,
):
    if not callback.message:
        await callback.answer()
        return

    if callback.message.chat.type not in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True,
        )
        return

    member = await callback.bot.get_chat_member(
        callback.message.chat.id,
        callback.from_user.id,
    )

    if member.status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        await callback.answer(
            "عزيزي\nليس مصرح لك بذلك",
            show_alert=True,
        )
        return

    key = chat_key(
        callback.message
    )

    current_mode = get_mode(key)

    requested_mode = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    if (
        current_mode == "default"
        and requested_mode == "default"
    ):
        new_mode = "voice"
    elif (
        current_mode == "voice"
        and requested_mode == "voice"
    ):
        new_mode = "default"
    else:
        new_mode = requested_mode

    set_mode(
        key,
        new_mode,
    )

    await callback.message.edit_reply_markup(
        reply_markup=mode_keyboard(
            new_mode
        )
    )

    await callback.answer()