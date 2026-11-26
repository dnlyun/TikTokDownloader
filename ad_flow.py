"""Provider-tolerant rewarded-ad state handling."""

import re


async def advance_ad_flow(pages, selectors, semantic_name) -> bool:
    """Click one eligible ad control across the supplied pages and frames."""
    for candidate in tuple(pages):
        try:
            if candidate.is_closed():
                continue
            frames = candidate.frames
        except Exception:
            continue
        for frame in frames:
            if await resume_close_confirmation(frame):
                return True
            countdown_active = await reward_countdown_active(frame)
            if not countdown_active and await click_reward_dismiss(frame):
                return True
            for selector in selectors:
                if countdown_active and any(
                    word in selector.lower() for word in ("dismiss", "close")
                ):
                    continue
                try:
                    for control in await frame.locator(selector).all():
                        if await control.is_visible():
                            await control.click(timeout=1000)
                            return True
                except Exception:
                    continue
            try:
                controls = await frame.get_by_role(
                    "button", name=semantic_name
                ).all()
                for control in controls:
                    if not countdown_active and await control.is_visible():
                        await control.click(timeout=1000)
                        return True
            except Exception:
                continue
    return False


async def click_reward_dismiss(frame) -> bool:
    """Immediately close an eligible rewarded ad, never its active wrapper."""
    try:
        control = frame.locator("#dismiss-button-element").first
        if not await control.is_visible() or not await control.is_enabled():
            return False
        await control.click(timeout=500)
        return True
    except Exception:
        return False


async def reward_countdown_active(frame) -> bool:
    """Return true while closing the rewarded ad would forfeit its reward."""
    try:
        locator = frame.locator("#dismiss-button")
        if await locator.count() == 0:
            return False
        text = (await locator.first.inner_text(timeout=250)).strip()
        match = re.search(r"(?:reward|close|skip)\s+in\s+(\d+)", text, re.I)
        # The provider can leave the label at 1 after enabling Close.
        return bool(match and int(match.group(1)) > 1)
    except Exception:
        return False


async def resume_close_confirmation(frame) -> bool:
    """Resume an ad after an accidental early-close confirmation."""
    try:
        dialogs = frame.locator("#close-confirmation-dialog")
        if await dialogs.count() == 0:
            return False
        active = await dialogs.first.evaluate("""element => {
            const style = getComputedStyle(element);
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                Number(style.opacity || 1) > 0.1 && style.pointerEvents !== 'none';
        }""", timeout=250)
        if not active:
            return False
        resumes = frame.locator("#resume-ad-button")
        if await resumes.count() == 0:
            return False
        resume = resumes.first
        if await resume.is_visible():
            await resume.click(timeout=1000)
            return True
    except Exception:
        pass
    return False
