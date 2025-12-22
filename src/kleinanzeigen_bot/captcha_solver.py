# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
import os
import tempfile
import urllib.request
import uuid
from typing import Final

import speech_recognition
from pydub import AudioSegment

from .utils import loggers
from .utils.web_scraping_mixin import Browser, By, Element, WebScrapingMixin

__all__ = [
    "CaptchaSolver",
]

LOG:Final[loggers.Logger] = loggers.get_logger(__name__)


class CaptchaSolver(WebScrapingMixin):

    def __init__(self, browser:Browser) -> None:
        super().__init__()

        self.browser = browser

        # Initialise speech recognition API object
        self._recognizer = speech_recognition.Recognizer()

        self.saved_page = self.page

    async def switch_to_frame(self, frame:Element) -> Element:
        self.saved_page = self.browser.tabs[0]

        iframe_tab = next((x for x in self.browser.targets if str(x.target.target_id) == str(frame.frame_id)), None)

        if iframe_tab:
            iframe_tab.websocket_url = iframe_tab.websocket_url.replace("iframe", "page")

        return iframe_tab

    async def solve_captcha(self, iframe:Element) -> bool:
        """
        Attempt to solve the reCAPTCHA challenge.
        """
        try:
            tab = await self.switch_to_frame(iframe)
            self.page = tab

            checkbox = await self.web_find(By.ID, "recaptcha-anchor", timeout=5)
            await checkbox.click()

            if await self.is_solved():
                return True

            self.page = self.saved_page
            await self.web_sleep()

            challenge_frame = await self.web_find(By.XPATH, '//iframe[contains(@src, "recaptcha") and contains(@src, "bframe")]', timeout=2)
            tab = await self.switch_to_frame(challenge_frame)
            self.page = tab

            await self.web_click(By.ID, "recaptcha-audio-button", timeout=2)
            await self.web_sleep()

            for x in range(3):
                LOG.debug("Try No. %d", x)
                try:
                    download_link = await self.web_find(By.CLASS_NAME, "rc-audiochallenge-tdownload-link",timeout=5)
                except TimeoutError:
                    LOG.debug("Google has detected automated queries. Try again later.", exc_info=True)
                    return False

                src = download_link.attrs["href"]

                response_text = await self._process_audio_challenge(src)
                if response_text == "False":
                    return False

                if response_text:
                    break

                await self.web_click(By.ID, "recaptcha-reload-button", timeout=5)

            await self.web_input(By.ID, "audio-response", response_text)
            await self.web_click(By.ID, "recaptcha-verify-button", timeout=5)
            await self.web_sleep()

            tab = await self.switch_to_frame(iframe)
            self.page = tab

            return await self.is_solved()

        except TimeoutError as ex:
            LOG.debug(ex, exc_info = True)
            return False

    async def _process_audio_challenge(self, audio_url: str) -> str:
        """
        Process audio challenge and return the recognized text.

        @param audio_url: URL of the audio file to process
        @return: recognized text from the audio file
        """

        # get temporary directory and create temporary files
        tmp_dir = tempfile.gettempdir()
        tmp_name = uuid.uuid4().hex

        mp3_file, wav_file = os.path.join(tmp_dir, f"{tmp_name}.mp3"), os.path.join(tmp_dir, f"{tmp_name}.wav")

        try:
            # url should start with http
            # checking src for little more security
            if not audio_url.lower().startswith("http"):
                raise ValueError("URL must start with 'http:'")

            urllib.request.urlretrieve(audio_url, mp3_file)  # nosec

            if os.path.getsize(mp3_file) == 0:
                return ""

            AudioSegment.from_mp3(mp3_file).export(wav_file, format="wav")

            with speech_recognition.AudioFile(wav_file) as source:
                # Disable dynamic energy threshold to avoid failed reCAPTCHA audio transcription due to static noise
                self._recognizer.dynamic_energy_threshold = False
                audio = self._recognizer.record(source)

            return "".join(self._recognizer.recognize_google(audio,language="de-DE"))

        except Exception as ex:
            LOG.debug(ex, exc_info=True)
            return "False"
        finally:
            for path in (mp3_file, wav_file):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    async def is_solved(self) -> bool:
        """
        Check if the captcha has been solved successfully.
        """
        try:
            checkbox = await self.web_find(By.ID, "recaptcha-anchor", timeout=5)

            if checkbox.attrs["aria-checked"] == "true":
                return True
        except TimeoutError:
            return False

        return False
