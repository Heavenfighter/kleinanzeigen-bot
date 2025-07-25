# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
import atexit, enum, json, os, re, signal, sys, textwrap  # isort: skip
import getopt  # pylint: disable=deprecated-module
import urllib.parse as urllib_parse
from gettext import gettext as _
from typing import Any, Final

import certifi, colorama, nodriver  # isort: skip
from ruamel.yaml import YAML
from wcmatch import glob

from . import extract, resources
from ._version import __version__
from .model.ad_model import MAX_DESCRIPTION_LENGTH, Ad, AdPartial
from .model.config_model import Config
from .update_checker import UpdateChecker
from .utils import dicts, error_handlers, loggers, misc
from .utils.exceptions import CaptchaEncountered
from .utils.files import abspath
from .utils.i18n import Locale, get_current_locale, pluralize, set_current_locale
from .utils.misc import ainput, ensure, is_frozen
from .utils.web_scraping_mixin import By, Element, Is, WebScrapingMixin

# W0406: possibly a bug, see https://github.com/PyCQA/pylint/issues/3933

LOG:Final[loggers.Logger] = loggers.get_logger(__name__)
LOG.setLevel(loggers.INFO)

colorama.just_fix_windows_console()


class AdUpdateStrategy(enum.Enum):
    REPLACE = enum.auto()
    MODIFY = enum.auto()


class KleinanzeigenBot(WebScrapingMixin):

    def __init__(self) -> None:

        # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/295
        # see https://github.com/pyinstaller/pyinstaller/issues/7229#issuecomment-1309383026
        os.environ["SSL_CERT_FILE"] = certifi.where()

        super().__init__()

        self.root_url = "https://www.kleinanzeigen.de"

        self.config:Config
        self.config_file_path = abspath("config.yaml")

        self.categories:dict[str, str] = {}

        self.file_log:loggers.LogFileHandle | None = None
        log_file_basename = is_frozen() and os.path.splitext(os.path.basename(sys.executable))[0] or self.__module__
        self.log_file_path:str | None = abspath(f"{log_file_basename}.log")

        self.command = "help"
        self.ads_selector = "due"
        self.keep_old_ads = False

    def __del__(self) -> None:
        if self.file_log:
            self.file_log.close()
            self.file_log = None
        self.close_browser_session()

    def get_version(self) -> str:
        return __version__

    async def run(self, args:list[str]) -> None:
        self.parse_args(args)
        try:
            match self.command:
                case "help":
                    self.show_help()
                    return
                case "version":
                    print(self.get_version())
                case "create-config":
                    self.create_default_config()
                    return
                case "verify":
                    self.configure_file_logging()
                    self.load_config()
                    # Check for updates on startup
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates()
                    self.load_ads()
                    LOG.info("############################################")
                    LOG.info("DONE: No configuration errors found.")
                    LOG.info("############################################")
                case "update-check":
                    self.configure_file_logging()
                    self.load_config()
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates(skip_interval_check = True)
                case "update-content-hash":
                    self.configure_file_logging()
                    self.load_config()
                    # Check for updates on startup
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates()
                    self.ads_selector = "all"
                    if ads := self.load_ads(exclude_ads_with_id = False):
                        self.update_content_hashes(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No active ads found.")
                        LOG.info("############################################")
                case "publish":
                    self.configure_file_logging()
                    self.load_config()
                    # Check for updates on startup
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates()

                    if not (self.ads_selector in {"all", "new", "due", "changed"} or
                            any(selector in self.ads_selector.split(",") for selector in ("all", "new", "due", "changed")) or
                            re.compile(r"\d+[,\d+]*").search(self.ads_selector)):
                        LOG.warning('You provided no ads selector. Defaulting to "due".')
                        self.ads_selector = "due"

                    if ads := self.load_ads():
                        await self.create_browser_session()
                        await self.login()
                        await self.publish_ads(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No new/outdated ads found.")
                        LOG.info("############################################")
                case "update":
                    self.configure_file_logging()
                    self.load_config()

                    if not (self.ads_selector in {"all", "changed"} or
                            any(selector in self.ads_selector.split(",") for selector in
                                ("all", "changed")) or
                            re.compile(r"\d+[,\d+]*").search(self.ads_selector)):
                        LOG.warning('You provided no ads selector. Defaulting to "changed".')
                        self.ads_selector = "changed"

                    if ads := self.load_ads():
                        await self.create_browser_session()
                        await self.login()
                        await self.update_ads(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No changed ads found.")
                        LOG.info("############################################")
                case "delete":
                    self.configure_file_logging()
                    self.load_config()
                    # Check for updates on startup
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates()
                    if ads := self.load_ads():
                        await self.create_browser_session()
                        await self.login()
                        await self.delete_ads(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No ads to delete found.")
                        LOG.info("############################################")
                case "download":
                    self.configure_file_logging()
                    # ad IDs depends on selector
                    if not (self.ads_selector in {"all", "new"} or re.compile(r"\d+[,\d+]*").search(self.ads_selector)):
                        LOG.warning('You provided no ads selector. Defaulting to "new".')
                        self.ads_selector = "new"
                    self.load_config()
                    # Check for updates on startup
                    checker = UpdateChecker(self.config)
                    checker.check_for_updates()
                    await self.create_browser_session()
                    await self.login()
                    await self.download_ads()

                case _:
                    LOG.error("Unknown command: %s", self.command)
                    sys.exit(2)
        finally:
            self.close_browser_session()

    def show_help(self) -> None:
        if is_frozen():
            exe = sys.argv[0]
        elif os.getenv("PDM_PROJECT_ROOT", ""):
            exe = "pdm run app"
        else:
            exe = "python -m kleinanzeigen_bot"

        if get_current_locale().language == "de":
            print(textwrap.dedent(f"""\
            Verwendung: {colorama.Fore.LIGHTMAGENTA_EX}{exe} BEFEHL [OPTIONEN]{colorama.Style.RESET_ALL}

            Befehle:
              publish  - (Wieder-)Veröffentlicht Anzeigen
              verify   - Überprüft die Konfigurationsdateien
              delete   - Löscht Anzeigen
              update   - Aktualisiert bestehende Anzeigen
              download - Lädt eine oder mehrere Anzeigen herunter
              update-check - Prüft auf verfügbare Updates
              update-content-hash - Berechnet den content_hash aller Anzeigen anhand der aktuellen ad_defaults neu;
                                    nach Änderungen an den config.yaml/ad_defaults verhindert es, dass alle Anzeigen als
                                    "geändert" gelten und neu veröffentlicht werden.
              create-config - Erstellt eine neue Standard-Konfigurationsdatei, falls noch nicht vorhanden
              --
              help     - Zeigt diese Hilfe an (Standardbefehl)
              version  - Zeigt die Version der Anwendung an

            Optionen:
              --ads=all|due|new|changed|<id(s)> (publish) - Gibt an, welche Anzeigen (erneut) veröffentlicht werden sollen (STANDARD: due)
                    Mögliche Werte:
                    * all: Veröffentlicht alle Anzeigen erneut, ignoriert republication_interval
                    * due: Veröffentlicht alle neuen Anzeigen und erneut entsprechend dem republication_interval
                    * new: Veröffentlicht nur neue Anzeigen (d.h. Anzeigen ohne ID in der Konfigurationsdatei)
                    * changed: Veröffentlicht nur Anzeigen, die seit der letzten Veröffentlichung geändert wurden
                    * <id(s)>: Gibt eine oder mehrere Anzeigen-IDs an, die veröffentlicht werden sollen, z. B. "--ads=1,2,3", ignoriert republication_interval
                    * Kombinationen: Sie können mehrere Selektoren mit Kommas kombinieren, z. B. "--ads=changed,due" um sowohl geänderte als auch
                      fällige Anzeigen zu veröffentlichen
              --ads=all|new|<id(s)> (download) - Gibt an, welche Anzeigen heruntergeladen werden sollen (STANDARD: new)
                    Mögliche Werte:
                    * all: Lädt alle Anzeigen aus Ihrem Profil herunter
                    * new: Lädt Anzeigen aus Ihrem Profil herunter, die lokal noch nicht gespeichert sind
                    * <id(s)>: Gibt eine oder mehrere Anzeigen-IDs zum Herunterladen an, z. B. "--ads=1,2,3"
              --ads=changed|<id(s)> (update) - Gibt an, welche Anzeigen aktualisiert werden sollen (STANDARD: changed)
                    Mögliche Werte:
                    * changed: Aktualisiert nur Anzeigen, die seit der letzten Veröffentlichung geändert wurden
                    * <id(s)>: Gibt eine oder mehrere Anzeigen-IDs zum Aktualisieren an, z. B. "--ads=1,2,3"
              --force           - Alias für '--ads=all'
              --keep-old        - Verhindert das Löschen alter Anzeigen bei erneuter Veröffentlichung
              --config=<PATH>   - Pfad zur YAML- oder JSON-Konfigurationsdatei (STANDARD: ./config.yaml)
              --logfile=<PATH>  - Pfad zur Protokolldatei (STANDARD: ./kleinanzeigen-bot.log)
              --lang=en|de      - Anzeigesprache (STANDARD: Systemsprache, wenn unterstützt, sonst Englisch)
              -v, --verbose     - Aktiviert detaillierte Ausgabe – nur nützlich zur Fehlerbehebung
            """.rstrip()))
        else:
            print(textwrap.dedent(f"""\
            Usage: {colorama.Fore.LIGHTMAGENTA_EX}{exe} COMMAND [OPTIONS]{colorama.Style.RESET_ALL}

            Commands:
              publish  - (re-)publishes ads
              verify   - verifies the configuration files
              delete   - deletes ads
              update   - updates published ads
              download - downloads one or multiple ads
              update-check - checks for available updates
              update-content-hash – recalculates each ad's content_hash based on the current ad_defaults;
                                    use this after changing config.yaml/ad_defaults to avoid every ad being marked "changed" and republished
              create-config - creates a new default configuration file if one does not exist
              --
              help     - displays this help (default command)
              version  - displays the application version

            Options:
              --ads=all|due|new|changed|<id(s)> (publish) - specifies which ads to (re-)publish (DEFAULT: due)
                    Possible values:
                    * all: (re-)publish all ads ignoring republication_interval
                    * due: publish all new ads and republish ads according the republication_interval
                    * new: only publish new ads (i.e. ads that have no id in the config file)
                    * changed: only publish ads that have been modified since last publication
                    * <id(s)>: provide one or several ads by ID to (re-)publish, like e.g. "--ads=1,2,3" ignoring republication_interval
                    * Combinations: You can combine multiple selectors with commas, e.g. "--ads=changed,due" to publish both changed and due ads
              --ads=all|new|<id(s)> (download) - specifies which ads to download (DEFAULT: new)
                    Possible values:
                    * all: downloads all ads from your profile
                    * new: downloads ads from your profile that are not locally saved yet
                    * <id(s)>: provide one or several ads by ID to download, like e.g. "--ads=1,2,3"
              --ads=changed|<id(s)> (update) - specifies which ads to update (DEFAULT: changed)
                    Possible values:
                    * changed: only update ads that have been modified since last publication
                    * <id(s)>: provide one or several ads by ID to update, like e.g. "--ads=1,2,3"
              --force           - alias for '--ads=all'
              --keep-old        - don't delete old ads on republication
              --config=<PATH>   - path to the config YAML or JSON file (DEFAULT: ./config.yaml)
              --logfile=<PATH>  - path to the logfile (DEFAULT: ./kleinanzeigen-bot.log)
              --lang=en|de      - display language (STANDARD: system language if supported, otherwise English)
              -v, --verbose     - enables verbose output - only useful when troubleshooting issues
            """.rstrip()))

    def parse_args(self, args:list[str]) -> None:
        try:
            options, arguments = getopt.gnu_getopt(args[1:], "hv", [
                "ads=",
                "config=",
                "force",
                "help",
                "keep-old",
                "logfile=",
                "lang=",
                "verbose"
            ])
        except getopt.error as ex:
            LOG.error(ex.msg)
            LOG.error("Use --help to display available options.")
            sys.exit(2)

        for option, value in options:
            match option:
                case "-h" | "--help":
                    self.show_help()
                    sys.exit(0)
                case "--config":
                    self.config_file_path = abspath(value)
                case "--logfile":
                    if value:
                        self.log_file_path = abspath(value)
                    else:
                        self.log_file_path = None
                case "--ads":
                    self.ads_selector = value.strip().lower()
                case "--force":
                    self.ads_selector = "all"
                case "--keep-old":
                    self.keep_old_ads = True
                case "--lang":
                    set_current_locale(Locale.of(value))
                case "-v" | "--verbose":
                    LOG.setLevel(loggers.DEBUG)
                    loggers.get_logger("nodriver").setLevel(loggers.INFO)

        match len(arguments):
            case 0:
                self.command = "help"
            case 1:
                self.command = arguments[0]
            case _:
                LOG.error("More than one command given: %s", arguments)
                sys.exit(2)

    def configure_file_logging(self) -> None:
        if not self.log_file_path:
            return
        if self.file_log:
            return

        LOG.info("Logging to [%s]...", self.log_file_path)
        self.file_log = loggers.configure_file_logging(self.log_file_path)

        LOG.info("App version: %s", self.get_version())
        LOG.info("Python version: %s", sys.version)

    def create_default_config(self) -> None:
        """
        Create a default config.yaml in the project root if it does not exist.
        If it exists, log an error and inform the user.
        """
        if os.path.exists(self.config_file_path):
            LOG.error("Config file %s already exists. Aborting creation.", self.config_file_path)
            return
        default_config = Config.model_construct()
        default_config.login.username = "changeme"  # noqa: S105 placeholder for default config, not a real username
        default_config.login.password = "changeme"  # noqa: S105 placeholder for default config, not a real password
        dicts.save_dict(
            self.config_file_path,
            default_config.model_dump(exclude_none = True, exclude = {"ad_defaults": {"description"}}),
            header = "# yaml-language-server: $schema=https://raw.githubusercontent.com/Second-Hand-Friends/kleinanzeigen-bot/refs/heads/main/schemas/config.schema.json"
        )

    def load_config(self) -> None:
        # write default config.yaml if config file does not exist
        if not os.path.exists(self.config_file_path):
            self.create_default_config()

        config_yaml = dicts.load_dict_if_exists(self.config_file_path, _("config"))
        self.config = Config.model_validate(config_yaml, strict = True, context = self.config_file_path)

        # load built-in category mappings
        self.categories = dicts.load_dict_from_module(resources, "categories.yaml", "categories")
        deprecated_categories = dicts.load_dict_from_module(resources, "categories_old.yaml", "categories")
        self.categories.update(deprecated_categories)
        if self.config.categories:
            self.categories.update(self.config.categories)
        LOG.info(" -> found %s", pluralize("category", self.categories))

        # populate browser_config object used by WebScrapingMixin
        self.browser_config.arguments = self.config.browser.arguments
        self.browser_config.binary_location = self.config.browser.binary_location
        self.browser_config.extensions = [abspath(item, relative_to = self.config_file_path) for item in self.config.browser.extensions]
        self.browser_config.use_private_window = self.config.browser.use_private_window
        if self.config.browser.user_data_dir:
            self.browser_config.user_data_dir = abspath(self.config.browser.user_data_dir, relative_to = self.config_file_path)
        self.browser_config.profile_name = self.config.browser.profile_name

    def __check_ad_republication(self, ad_cfg:Ad, ad_file_relative:str) -> bool:
        """
        Check if an ad needs to be republished based on republication interval.
        Note:  This method does not check for content changes. Use __check_ad_changed for that.

        Returns:
            True if the ad should be republished based on the interval.
        """
        if ad_cfg.updated_on:
            last_updated_on = ad_cfg.updated_on
        elif ad_cfg.created_on:
            last_updated_on = ad_cfg.created_on
        else:
            return True

        if not last_updated_on:
            return True

        # Check republication interval
        ad_age = misc.now() - last_updated_on
        if ad_age.days <= ad_cfg.republication_interval:
            LOG.info(
                " -> SKIPPED: ad [%s] was last published %d days ago. republication is only required every %s days",
                ad_file_relative,
                ad_age.days,
                ad_cfg.republication_interval
            )
            return False

        return True

    def __check_ad_changed(self, ad_cfg:Ad, ad_cfg_orig:dict[str, Any], ad_file_relative:str) -> bool:
        """
        Check if an ad has been changed since last publication.

        Returns:
            True if the ad has been changed.
        """
        if not ad_cfg.id:
            # New ads are not considered "changed"
            return False

        # Calculate hash on original config to match what was stored
        current_hash = AdPartial.model_validate(ad_cfg_orig).update_content_hash().content_hash
        stored_hash = ad_cfg_orig.get("content_hash")

        LOG.debug("Hash comparison for [%s]:", ad_file_relative)
        LOG.debug("    Stored hash: %s", stored_hash)
        LOG.debug("    Current hash: %s", current_hash)

        if stored_hash and current_hash != stored_hash:
            LOG.info("Changes detected in ad [%s], will republish", ad_file_relative)
            # Update hash in original configuration
            ad_cfg_orig["content_hash"] = current_hash
            return True

        return False

    def load_ads(self, *, ignore_inactive:bool = True, exclude_ads_with_id:bool = True) -> list[tuple[str, Ad, dict[str, Any]]]:
        """
        Load and validate all ad config files, optionally filtering out inactive or already-published ads.

        Args:
            ignore_inactive (bool):
                Skip ads with `active=False`.
            exclude_ads_with_id (bool):
                Skip ads whose raw data already contains an `id`, i.e. was published before.

        Returns:
            list[tuple[str, Ad, dict[str, Any]]]:
            Tuples of (file_path, validated Ad model, original raw data).
        """
        LOG.info("Searching for ad config files...")

        ad_files:dict[str, str] = {}
        data_root_dir = os.path.dirname(self.config_file_path)
        for file_pattern in self.config.ad_files:
            for ad_file in glob.glob(file_pattern, root_dir = data_root_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                if not str(ad_file).endswith("ad_fields.yaml"):
                    ad_files[abspath(ad_file, relative_to = data_root_dir)] = ad_file
        LOG.info(" -> found %s", pluralize("ad config file", ad_files))
        if not ad_files:
            return []

        ids = []
        use_specific_ads = False
        selectors = self.ads_selector.split(",")

        if re.compile(r"\d+[,\d+]*").search(self.ads_selector):
            ids = [int(n) for n in self.ads_selector.split(",")]
            use_specific_ads = True
            LOG.info("Start fetch task for the ad(s) with id(s):")
            LOG.info(" | ".join([str(id_) for id_ in ids]))

        ads = []
        for ad_file, ad_file_relative in sorted(ad_files.items()):
            ad_cfg_orig:dict[str, Any] = dicts.load_dict(ad_file, "ad")
            ad_cfg:Ad = self.load_ad(ad_cfg_orig)

            if ignore_inactive and not ad_cfg.active:
                LOG.info(" -> SKIPPED: inactive ad [%s]", ad_file_relative)
                continue

            if use_specific_ads:
                if ad_cfg.id not in ids:
                    LOG.info(" -> SKIPPED: ad [%s] is not in list of given ids.", ad_file_relative)
                    continue
            else:
                # Check if ad should be included based on selectors
                should_include = False

                # Check for 'changed' selector
                if "changed" in selectors and self.__check_ad_changed(ad_cfg, ad_cfg_orig, ad_file_relative):
                    should_include = True

                # Check for 'new' selector
                if "new" in selectors and (not ad_cfg.id or not exclude_ads_with_id):
                    should_include = True
                elif "new" in selectors and ad_cfg.id and exclude_ads_with_id:
                    LOG.info(" -> SKIPPED: ad [%s] is not new. already has an id assigned.", ad_file_relative)

                # Check for 'due' selector
                if "due" in selectors:
                    # For 'due' selector, check if the ad is due for republication based on interval
                    if self.__check_ad_republication(ad_cfg, ad_file_relative):
                        should_include = True

                # Check for 'all' selector (always include)
                if "all" in selectors:
                    should_include = True

                if not should_include:
                    continue

            ensure(self.__get_description(ad_cfg, with_affixes = False), f"-> property [description] not specified @ [{ad_file}]")
            self.__get_description(ad_cfg, with_affixes = True)  # validates complete description

            if ad_cfg.category:
                resolved_category_id = self.categories.get(ad_cfg.category)
                if not resolved_category_id and ">" in ad_cfg.category:
                    # this maps actually to the sonstiges/weiteres sub-category
                    parent_category = ad_cfg.category.rpartition(">")[0].strip()
                    resolved_category_id = self.categories.get(parent_category)
                    if resolved_category_id:
                        LOG.warning(
                            "Category [%s] unknown. Using category [%s] with ID [%s] instead.",
                            ad_cfg.category, parent_category, resolved_category_id)

                if resolved_category_id:
                    ad_cfg.category = resolved_category_id

            if ad_cfg.images:
                images = []
                ad_dir = os.path.dirname(ad_file)
                for image_pattern in ad_cfg.images:
                    pattern_images = set()
                    for image_file in glob.glob(image_pattern, root_dir = ad_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                        _, image_file_ext = os.path.splitext(image_file)
                        ensure(image_file_ext.lower() in {".gif", ".jpg", ".jpeg", ".png"}, f"Unsupported image file type [{image_file}]")
                        if os.path.isabs(image_file):
                            pattern_images.add(image_file)
                        else:
                            pattern_images.add(abspath(image_file, relative_to = ad_file))
                    images.extend(sorted(pattern_images))
                ensure(images or not ad_cfg.images, f"No images found for given file patterns {ad_cfg.images} at {ad_dir}")
                ad_cfg.images = list(dict.fromkeys(images))

            ads.append((
                ad_file,
                ad_cfg,
                ad_cfg_orig
            ))

        LOG.info("Loaded %s", pluralize("ad", ads))
        return ads

    def load_ad(self, ad_cfg_orig:dict[str, Any]) -> Ad:
        return AdPartial.model_validate(ad_cfg_orig).to_ad(self.config.ad_defaults)

    async def check_and_wait_for_captcha(self, *, is_login_page:bool = True) -> None:
        try:
            await self.web_find(By.CSS_SELECTOR,
                                "iframe[name^='a-'][src^='https://www.google.com/recaptcha/api2/anchor?']", timeout = 2)

            if not is_login_page and self.config.captcha.auto_restart:
                LOG.warning("Captcha recognized - auto-restart enabled, abort run...")
                raise CaptchaEncountered(misc.parse_duration(self.config.captcha.restart_delay))

            LOG.warning("############################################")
            LOG.warning("# Captcha present! Please solve the captcha.")
            LOG.warning("############################################")

            if not is_login_page:
                await self.web_scroll_page_down()

            await ainput(_("Press a key to continue..."))
        except TimeoutError:
            pass

    async def login(self) -> None:
        LOG.info("Checking if already logged in...")
        await self.web_open(f"{self.root_url}")

        if await self.is_logged_in():
            LOG.info("Already logged in as [%s]. Skipping login.", self.config.login.username)
            return

        LOG.info("Opening login page...")
        await self.web_open(f"{self.root_url}/m-einloggen.html?targetUrl=/")

        await self.fill_login_data_and_send()
        await self.handle_after_login_logic()

        # Sometimes a second login is required
        if not await self.is_logged_in():
            await self.fill_login_data_and_send()
            await self.handle_after_login_logic()

    async def fill_login_data_and_send(self) -> None:
        LOG.info("Logging in as [%s]...", self.config.login.username)
        await self.web_input(By.ID, "login-email", self.config.login.username)

        # clearing password input in case browser has stored login data set
        await self.web_input(By.ID, "login-password", "")
        await self.web_input(By.ID, "login-password", self.config.login.password)

        await self.check_and_wait_for_captcha(is_login_page = True)

        await self.web_click(By.CSS_SELECTOR, "form#login-form button[type='submit']")

    async def handle_after_login_logic(self) -> None:
        try:
            await self.web_find(By.TEXT, "Wir haben dir gerade einen 6-stelligen Code für die Telefonnummer", timeout = 4)
            LOG.warning("############################################")
            LOG.warning("# Device verification message detected. Please follow the instruction displayed in the Browser.")
            LOG.warning("############################################")
            await ainput("Press ENTER when done...")
        except TimeoutError:
            pass

        try:
            LOG.info("Handling GDPR disclaimer...")
            await self.web_find(By.ID, "gdpr-banner-accept", timeout = 10)
            await self.web_click(By.ID, "gdpr-banner-cmp-button")
            await self.web_click(By.XPATH, "//div[@id='ConsentManagementPage']//*//button//*[contains(., 'Alle ablehnen und fortfahren')]", timeout = 10)
        except TimeoutError:
            pass

    async def is_logged_in(self) -> bool:
        try:
            # Try to find the standard element first
            user_info = await self.web_text(By.CLASS_NAME, "mr-medium")
            if self.config.login.username.lower() in user_info.lower():
                return True
        except TimeoutError:
            try:
                # If standard element not found, try the alternative
                user_info = await self.web_text(By.ID, "user-email")
                if self.config.login.username.lower() in user_info.lower():
                    return True
            except TimeoutError:
                return False
        return False

    async def delete_ads(self, ad_cfgs:list[tuple[str, Ad, dict[str, Any]]]) -> None:
        count = 0

        published_ads = json.loads(
            (await self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT"))["content"])["ads"]

        for (ad_file, ad_cfg, _ad_cfg_orig) in ad_cfgs:
            count += 1
            LOG.info("Processing %s/%s: '%s' from [%s]...", count, len(ad_cfgs), ad_cfg.title, ad_file)
            await self.delete_ad(ad_cfg, published_ads, delete_old_ads_by_title = self.config.publishing.delete_old_ads_by_title)
            await self.web_sleep()

        LOG.info("############################################")
        LOG.info("DONE: Deleted %s", pluralize("ad", count))
        LOG.info("############################################")

    async def delete_ad(self, ad_cfg:Ad, published_ads:list[dict[str, Any]], *, delete_old_ads_by_title:bool) -> bool:
        LOG.info("Deleting ad '%s' if already present...", ad_cfg.title)

        await self.web_open(f"{self.root_url}/m-meine-anzeigen.html")
        csrf_token_elem = await self.web_find(By.CSS_SELECTOR, "meta[name=_csrf]")
        csrf_token = csrf_token_elem.attrs["content"]
        ensure(csrf_token is not None, "Expected CSRF Token not found in HTML content!")

        if delete_old_ads_by_title:

            for published_ad in published_ads:
                published_ad_id = int(published_ad.get("id", -1))
                published_ad_title = published_ad.get("title", "")
                if ad_cfg.id == published_ad_id or ad_cfg.title == published_ad_title:
                    LOG.info(" -> deleting %s '%s'...", published_ad_id, published_ad_title)
                    await self.web_request(
                        url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={published_ad_id}",
                        method = "POST",
                        headers = {"x-csrf-token": csrf_token}
                    )
        elif ad_cfg.id:
            await self.web_request(
                url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={ad_cfg.id}",
                method = "POST",
                headers = {"x-csrf-token": csrf_token},
                valid_response_codes = [200, 404]
            )

        await self.web_sleep()
        ad_cfg.id = None
        return True

    async def publish_ads(self, ad_cfgs:list[tuple[str, Ad, dict[str, Any]]]) -> None:
        count = 0

        published_ads = json.loads(
            (await self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT"))["content"])["ads"]

        for (ad_file, ad_cfg, ad_cfg_orig) in ad_cfgs:
            LOG.info("Processing %s/%s: '%s' from [%s]...", count + 1, len(ad_cfgs), ad_cfg.title, ad_file)

            if [x for x in published_ads if x["id"] == ad_cfg.id and x["state"] == "paused"]:
                LOG.info("Skipping because ad is reserved")
                continue

            count += 1

            await self.publish_ad(ad_file, ad_cfg, ad_cfg_orig, published_ads, AdUpdateStrategy.REPLACE)
            await self.web_await(lambda: self.web_check(By.ID, "checking-done", Is.DISPLAYED), timeout = 5 * 60)

            if self.config.publishing.delete_old_ads == "AFTER_PUBLISH" and not self.keep_old_ads:
                await self.delete_ad(ad_cfg, published_ads, delete_old_ads_by_title = False)

        LOG.info("############################################")
        LOG.info("DONE: (Re-)published %s", pluralize("ad", count))
        LOG.info("############################################")

    async def publish_ad(self, ad_file:str, ad_cfg:Ad, ad_cfg_orig:dict[str, Any], published_ads:list[dict[str, Any]],
                         mode:AdUpdateStrategy = AdUpdateStrategy.REPLACE) -> None:
        """
        @param ad_cfg: the effective ad config (i.e. with default values applied etc.)
        @param ad_cfg_orig: the ad config as present in the YAML file
        @param published_ads: json list of published ads
        @param mode: the mode of ad editing, either publishing a new or updating an existing ad
        """

        if mode == AdUpdateStrategy.REPLACE:
            if self.config.publishing.delete_old_ads == "BEFORE_PUBLISH" and not self.keep_old_ads:
                await self.delete_ad(ad_cfg, published_ads, delete_old_ads_by_title = self.config.publishing.delete_old_ads_by_title)

            LOG.info("Publishing ad '%s'...", ad_cfg.title)
            await self.web_open(f"{self.root_url}/p-anzeige-aufgeben-schritt2.html")
        else:
            LOG.info("Updating ad '%s'...", ad_cfg.title)
            await self.web_open(f"{self.root_url}/p-anzeige-bearbeiten.html?adId={ad_cfg.id}")

        if loggers.is_debug(LOG):
            LOG.debug(" -> effective ad meta:")
            YAML().dump(ad_cfg.model_dump(), sys.stdout)

        if ad_cfg.type == "WANTED":
            await self.web_click(By.ID, "adType2")

        #############################
        # set title
        #############################
        await self.web_input(By.ID, "postad-title", ad_cfg.title)

        #############################
        # set category
        #############################
        await self.__set_category(ad_cfg.category, ad_file)

        #############################
        # set special attributes
        #############################
        await self.__set_special_attributes(ad_cfg)

        #############################
        # set shipping type/options/costs
        #############################
        shipping_type = ad_cfg.shipping_type
        if shipping_type != "NOT_APPLICABLE":
            if ad_cfg.type == "WANTED":
                # special handling for ads of type WANTED since shipping is a special attribute for these
                if shipping_type in {"PICKUP", "SHIPPING"}:
                    shipping_value = "ja" if shipping_type == "SHIPPING" else "nein"
                    try:
                        await self.web_select(By.XPATH, "//select[contains(@id, '.versand_s')]", shipping_value)
                    except TimeoutError:
                        LOG.warning("Failed to set shipping attribute for type '%s'!", shipping_type)
            else:
                await self.__set_shipping(ad_cfg, mode)
        else:
            LOG.debug("Shipping step skipped - reason: NOT_APPLICABLE")

        #############################
        # set price
        #############################
        price_type = ad_cfg.price_type
        if price_type != "NOT_APPLICABLE":
            try:
                await self.web_select(By.CSS_SELECTOR, "select#price-type-react, select#micro-frontend-price-type, select#priceType", price_type)
            except TimeoutError:
                pass
            if ad_cfg.price:
                if mode == AdUpdateStrategy.MODIFY:
                    # we have to clear the input, otherwise input gets appended
                    await self.web_input(By.CSS_SELECTOR,
                                     "input#post-ad-frontend-price, input#micro-frontend-price, input#pstad-price", "")
                await self.web_input(By.CSS_SELECTOR, "input#post-ad-frontend-price, input#micro-frontend-price, input#pstad-price", str(ad_cfg.price))

        #############################
        # set sell_directly
        #############################
        sell_directly = ad_cfg.sell_directly
        try:
            if ad_cfg.shipping_type == "SHIPPING":
                if sell_directly and ad_cfg.shipping_options and price_type in {"FIXED", "NEGOTIABLE"}:
                    if not await self.web_check(By.ID, "radio-buy-now-yes", Is.SELECTED):
                        await self.web_click(By.ID, "radio-buy-now-yes")
                elif not await self.web_check(By.ID, "radio-buy-now-no", Is.SELECTED):
                    await self.web_click(By.ID, "radio-buy-now-no")
        except TimeoutError as ex:
            LOG.debug(ex, exc_info = True)

        #############################
        # set description
        #############################
        description = self.__get_description(ad_cfg, with_affixes = True)
        await self.web_execute("document.querySelector('#pstad-descrptn').value = `" + description.replace("`", "'") + "`")

        #############################
        # set contact zipcode
        #############################
        if ad_cfg.contact.zipcode:
            await self.web_input(By.ID, "pstad-zip", ad_cfg.contact.zipcode)
            # Set city if location is specified
            if ad_cfg.contact.location:
                try:
                    await self.web_sleep(1)  # Wait for city dropdown to populate
                    options = await self.web_find_all(By.CSS_SELECTOR, "#pstad-citychsr option")

                    for option in options:
                        if option.text == ad_cfg.contact.location:
                            await self.web_select(By.ID, "pstad-citychsr", option.attrs.value)
                            break
                except TimeoutError:
                    LOG.debug("Could not set city from location")

        #############################
        # set contact street
        #############################
        if ad_cfg.contact.street:
            try:
                if await self.web_check(By.ID, "pstad-street", Is.DISABLED):
                    await self.web_click(By.ID, "addressVisibility")
                    await self.web_sleep()
            except TimeoutError:
                # ignore
                pass
            await self.web_input(By.ID, "pstad-street", ad_cfg.contact.street)

        #############################
        # set contact name
        #############################
        if ad_cfg.contact.name and not await self.web_check(By.ID, "postad-contactname", Is.READONLY):
            await self.web_input(By.ID, "postad-contactname", ad_cfg.contact.name)

        #############################
        # set contact phone
        #############################
        if ad_cfg.contact.phone:
            if await self.web_check(By.ID, "postad-phonenumber", Is.DISPLAYED):
                try:
                    if await self.web_check(By.ID, "postad-phonenumber", Is.DISABLED):
                        await self.web_click(By.ID, "phoneNumberVisibility")
                        await self.web_sleep()
                except TimeoutError:
                    # ignore
                    pass
                await self.web_input(By.ID, "postad-phonenumber", ad_cfg.contact.phone)

        if mode == AdUpdateStrategy.MODIFY:
            #############################
            # delete previous images because we don't know which have changed
            #############################
            img_items = await self.web_find_all(By.CSS_SELECTOR,
                                                "ul#j-pictureupload-thumbnails > li.ui-sortable-handle")
            for element in img_items:
                btn = await self.web_find(By.CSS_SELECTOR, "button.pictureupload-thumbnails-remove", parent = element)
                await btn.click()

        #############################
        # upload images
        #############################
        await self.__upload_images(ad_cfg)

        #############################
        # wait for captcha
        #############################
        await self.check_and_wait_for_captcha(is_login_page = False)

        #############################
        # submit
        #############################
        try:
            await self.web_click(By.ID, "pstad-submit")
        except TimeoutError:
            # https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/40
            await self.web_click(By.XPATH, "//fieldset[@id='postad-publish']//*[contains(., 'Anzeige aufgeben')]")
            await self.web_click(By.ID, "imprint-guidance-submit")

        # check for no image question
        try:
            image_hint_xpath = '//*[contains(@class, "ModalDialog--Actions")]//button[contains(., "Ohne Bild veröffentlichen")]'
            if not ad_cfg.images and await self.web_check(By.XPATH, image_hint_xpath, Is.DISPLAYED):
                await self.web_click(By.XPATH, image_hint_xpath)
        except TimeoutError:
            pass  # nosec

        #############################
        # wait for payment form if commercial account is used
        #############################
        try:
            await self.web_find(By.ID, "myftr-shppngcrt-frm", timeout = 2)

            LOG.warning("############################################")
            LOG.warning("# Payment form detected! Please proceed with payment.")
            LOG.warning("############################################")
            await self.web_scroll_page_down()
            input(_("Press a key to continue..."))
        except TimeoutError:
            pass

        await self.web_await(lambda: "p-anzeige-aufgeben-bestaetigung.html?adId=" in self.page.url, timeout = 20)

        # extract the ad id from the URL's query parameter
        current_url_query_params = urllib_parse.parse_qs(urllib_parse.urlparse(self.page.url).query)
        ad_id = int(current_url_query_params.get("adId", [])[0])
        ad_cfg_orig["id"] = ad_id

        # check for approval message
        try:
            approval_link_xpath = '//*[contains(@id, "not-completed")]//a[contains(@class, "to-my-ads-link")]'
            if await self.web_check(By.XPATH, approval_link_xpath, Is.DISPLAYED):
                await self.web_click(By.XPATH, approval_link_xpath)
        except TimeoutError:
            pass  # nosec

        # Update content hash after successful publication
        # Calculate hash on original config to ensure consistent comparison on restart
        ad_cfg_orig["content_hash"] = AdPartial.model_validate(ad_cfg_orig).update_content_hash().content_hash
        ad_cfg_orig["updated_on"] = misc.now().isoformat(timespec = "seconds")
        if not ad_cfg.created_on and not ad_cfg.id:
            ad_cfg_orig["created_on"] = ad_cfg_orig["updated_on"]

        if mode == AdUpdateStrategy.REPLACE:
            LOG.info(" -> SUCCESS: ad published with ID %s", ad_id)
        else:
            LOG.info(" -> SUCCESS: ad updated with ID %s", ad_id)

        dicts.save_dict(ad_file, ad_cfg_orig)

    async def update_ads(self, ad_cfgs:list[tuple[str, Ad, dict[str, Any]]]) -> None:
        """
        Updates a list of ads.
        The list gets filtered, so that only already published ads will be updated.
        Calls publish_ad in MODIFY mode.

        Args:
            ad_cfgs: List of ad configurations

        Returns:
            None
        """
        count = 0

        published_ads = json.loads(
            (await self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT"))["content"])["ads"]

        for (ad_file, ad_cfg, ad_cfg_orig) in ad_cfgs:
            ad = next((ad for ad in published_ads if ad["id"] == ad_cfg.id), None)

            if not ad:
                continue

            LOG.info("Processing %s/%s: '%s' from [%s]...", count + 1, len(ad_cfgs), ad_cfg.title, ad_file)
            if ad["state"] == "paused":
                LOG.info("Skipping because ad is reserved")
                continue

            count += 1

            await self.publish_ad(ad_file, ad_cfg, ad_cfg_orig, published_ads, AdUpdateStrategy.MODIFY)
            await self.web_await(lambda: self.web_check(By.ID, "checking-done", Is.DISPLAYED), timeout = 5 * 60)

        LOG.info("############################################")
        LOG.info("DONE: updated %s", pluralize("ad", count))
        LOG.info("############################################")

    async def __set_condition(self, condition_value:str) -> None:
        condition_mapping = {
            "new_with_tag": "Neu mit Etikett",
            "new": "Neu",
            "like_new": "Sehr Gut",
            "good": "Gut",
            "ok": "Gut",
            "alright": "In Ordnung",
            "defect": "Defekt",
        }
        mapped_condition = condition_mapping.get(condition_value)

        try:
            # Open condition dialog
            await self.web_click(By.XPATH, '//*[@id="j-post-listing-frontend-conditions"]//button[contains(@class, "SelectionButton")]')
        except TimeoutError:
            LOG.debug("Unable to open condition dialog and select condition [%s]", condition_value, exc_info = True)
            return

        try:
            # Click radio button
            await self.web_click(By.CSS_SELECTOR, f'.SingleSelectionItem--Main input[type=radio][data-testid="{mapped_condition}"]')
        except TimeoutError:
            LOG.debug("Unable to select condition [%s]", condition_value, exc_info = True)

        try:
            # Click accept button
            await self.web_click(By.XPATH, '//*[contains(@id, "j-post-listing-frontend-conditions")]//dialog//button[contains(., "Bestätigen")]')
        except TimeoutError as ex:
            raise TimeoutError(_("Unable to close condition dialog!")) from ex

    async def __set_category(self, category:str | None, ad_file:str) -> None:
        # click on something to trigger automatic category detection
        await self.web_click(By.ID, "pstad-descrptn")

        is_category_auto_selected = False
        try:
            if await self.web_text(By.ID, "postad-category-path"):
                is_category_auto_selected = True
        except TimeoutError:
            pass

        if category:
            await self.web_sleep()  # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/39
            await self.web_click(By.ID, "pstad-lnk-chngeCtgry")
            await self.web_find(By.ID, "postad-step1-sbmt")

            category_url = f"{self.root_url}/p-kategorie-aendern.html#?path={category}"
            await self.web_open(category_url)
            await self.web_click(By.XPATH, "//*[@id='postad-step1-sbmt']/button")
        else:
            ensure(is_category_auto_selected, f"No category specified in [{ad_file}] and automatic category detection failed")

    async def __set_special_attributes(self, ad_cfg:Ad) -> None:
        if not ad_cfg.special_attributes:
            return

        LOG.debug("Found %i special attributes", len(ad_cfg.special_attributes))
        for special_attribute_key, special_attribute_value in ad_cfg.special_attributes.items():

            if special_attribute_key == "condition_s":
                await self.__set_condition(special_attribute_value)
                continue

            LOG.debug("Setting special attribute [%s] to [%s]...", special_attribute_key, special_attribute_value)
            try:
                # if the <select> element exists but is inside an invisible container, make the container visible
                select_container_xpath = f"//div[@class='l-row' and descendant::select[@id='{special_attribute_key}']]"
                if not await self.web_check(By.XPATH, select_container_xpath, Is.DISPLAYED):
                    await (await self.web_find(By.XPATH, select_container_xpath)).apply("elem => elem.singleNodeValue.style.display = 'block'")
            except TimeoutError:
                pass  # nosec

            try:
                # finding element by name cause id are composed sometimes eg. autos.marke_s+autos.model_s for Modell by cars
                special_attr_elem = await self.web_find(By.XPATH, f"//*[contains(@name, '{special_attribute_key}')]")
            except TimeoutError as ex:
                LOG.debug("Attribute field '%s' could not be found.", special_attribute_key)
                raise TimeoutError(f"Failed to set special attribute [{special_attribute_key}] (not found)") from ex

            try:
                elem_id = special_attr_elem.attrs.id
                if special_attr_elem.local_name == "select":
                    LOG.debug("Attribute field '%s' seems to be a select...", special_attribute_key)
                    await self.web_select(By.ID, elem_id, special_attribute_value)
                elif special_attr_elem.attrs.type == "checkbox":
                    LOG.debug("Attribute field '%s' seems to be a checkbox...", special_attribute_key)
                    await self.web_click(By.ID, elem_id)
                else:
                    LOG.debug("Attribute field '%s' seems to be a text input...", special_attribute_key)
                    await self.web_input(By.ID, elem_id, special_attribute_value)
            except TimeoutError as ex:
                LOG.debug("Attribute field '%s' is not of kind radio button.", special_attribute_key)
                raise TimeoutError(f"Failed to set special attribute [{special_attribute_key}]") from ex
            LOG.debug("Successfully set attribute field [%s] to [%s]...", special_attribute_key, special_attribute_value)

    async def __set_shipping(self, ad_cfg:Ad, mode:AdUpdateStrategy = AdUpdateStrategy.REPLACE) -> None:
        if ad_cfg.shipping_type == "PICKUP":
            try:
                await self.web_click(By.ID, "radio-pickup")
            except TimeoutError as ex:
                LOG.debug(ex, exc_info = True)
        elif ad_cfg.shipping_options:
            await self.web_click(By.XPATH, '//button//span[contains(., "Versandmethoden auswählen")]')

            if mode == AdUpdateStrategy.MODIFY:
                try:
                    # when "Andere Versandmethoden" is not available, go back and start over new
                    await self.web_find(By.XPATH, '//dialog//button[contains(., "Andere Versandmethoden")]', timeout = 2)
                except TimeoutError:
                    await self.web_click(By.XPATH, '//dialog//button[contains(., "Zurück")]')

            await self.web_click(By.XPATH, '//dialog//button[contains(., "Andere Versandmethoden")]')
            await self.__set_shipping_options(ad_cfg)
        else:
            special_shipping_selector = '//select[contains(@id, ".versand_s")]'
            if await self.web_check(By.XPATH, special_shipping_selector, Is.DISPLAYED):
                # try to set special attribute selector (then we have a commercial account)
                shipping_value = "ja" if ad_cfg.shipping_type == "SHIPPING" else "nein"
                await self.web_select(By.XPATH, special_shipping_selector, shipping_value)
            else:
                try:
                    # no options. only costs. Set custom shipping cost
                    if ad_cfg.shipping_costs is not None:
                        await self.web_click(By.XPATH,
                                             '//button//span[contains(., "Versandmethoden auswählen")]')

                        try:
                            # when "Andere Versandmethoden" is not available, then we are already on the individual page
                            await self.web_click(By.XPATH,
                                                 '//dialog//button[contains(., "Andere Versandmethoden")]')
                        except TimeoutError:
                            pass

                        try:
                            # only click on "Individueller Versand" when "IndividualShippingInput" is not available, otherwise its already checked
                            # (important for mode = UPDATE)
                            await self.web_find(By.XPATH,
                                                '//input[contains(@placeholder, "Versandkosten (optional)")]',
                                                timeout = 2)
                        except TimeoutError:
                            await self.web_click(By.XPATH, '//*[contains(@id, "INDIVIDUAL") and contains(@data-testid, "Individueller Versand")]')

                        await self.web_input(By.XPATH, '//input[contains(@placeholder, "Versandkosten (optional)")]',
                                             str.replace(str(ad_cfg.shipping_costs), ".", ","))
                        await self.web_click(By.XPATH, '//dialog//button[contains(., "Fertig")]')
                except TimeoutError as ex:
                    LOG.debug(ex, exc_info = True)
                    raise TimeoutError(_("Unable to close shipping dialog!")) from ex

    async def __set_shipping_options(self, ad_cfg:Ad) -> None:
        if not ad_cfg.shipping_options:
            return

        shipping_options_mapping = {
            "DHL_2": ("Klein", "SMALL", "Paket 2 kg"),
            "Hermes_Päckchen": ("Klein", "SMALL", "Päckchen"),
            "Hermes_S": ("Klein", "SMALL", "S-Paket"),
            "DHL_5": ("Mittel", "MEDIUM", "Paket 5 kg"),
            "Hermes_M": ("Mittel", "MEDIUM", "M-Paket"),
            "DHL_10": ("Groß", "LARGE", "Paket 10 kg"),
            "DHL_20": ("Groß", "LARGE", "Paket 20 kg"),
            "DHL_31,5": ("Groß", "LARGE", "Paket 31,5 kg"),
            "Hermes_L": ("Groß", "LARGE", "L-Paket"),
        }
        try:
            mapped_shipping_options = [shipping_options_mapping[option] for option in set(ad_cfg.shipping_options)]
        except KeyError as ex:
            raise KeyError(f"Unknown shipping option(s), please refer to the documentation/README: {ad_cfg.shipping_options}") from ex

        shipping_sizes, shipping_selector, shipping_packages = zip(*mapped_shipping_options, strict = False)

        try:
            shipping_size, = set(shipping_sizes)
        except ValueError as ex:
            raise ValueError("You can only specify shipping options for one package size!") from ex

        try:
            shipping_radio_selector = shipping_selector[0]
            shipping_size_radio = await self.web_find(By.ID, f"radio-button-{shipping_radio_selector}")
            shipping_size_radio_is_checked = hasattr(shipping_size_radio.attrs, "checked")

            if shipping_size_radio_is_checked:
                unwanted_shipping_packages = [
                    package for size, selector, package in shipping_options_mapping.values()
                    if size == shipping_size and package not in shipping_packages
                ]
                to_be_clicked_shipping_packages = unwanted_shipping_packages
            else:
                await self.web_click(By.ID, f"radio-button-{shipping_radio_selector}")
                to_be_clicked_shipping_packages = list(shipping_packages)

            await self.web_click(By.XPATH, '//dialog//button[contains(., "Weiter")]')

            for shipping_package in to_be_clicked_shipping_packages:
                try:
                    await self.web_click(
                        By.XPATH,
                        f'//dialog//input[contains(@data-testid, "{shipping_package}")]')
                except TimeoutError as ex:
                    LOG.debug(ex, exc_info = True)

        except TimeoutError as ex:
            LOG.debug(ex, exc_info = True)
        try:
            # Click apply button
            await self.web_click(By.XPATH, '//dialog//button[contains(., "Fertig")]')
        except TimeoutError as ex:
            raise TimeoutError(_("Unable to close shipping dialog!")) from ex

    async def __upload_images(self, ad_cfg:Ad) -> None:
        if not ad_cfg.images:
            return

        LOG.info(" -> found %s", pluralize("image", ad_cfg.images))
        image_upload:Element = await self.web_find(By.CSS_SELECTOR, "input[type=file]")

        for image in ad_cfg.images:
            LOG.info(" -> uploading image [%s]", image)
            await image_upload.send_file(image)
            await self.web_sleep()

    async def download_ads(self) -> None:
        """
        Determines which download mode was chosen with the arguments, and calls the specified download routine.
        This downloads either all, only unsaved (new), or specific ads given by ID.
        """

        ad_extractor = extract.AdExtractor(self.browser, self.config)

        # use relevant download routine
        if self.ads_selector in {"all", "new"}:  # explore ads overview for these two modes
            LOG.info("Scanning your ad overview...")
            own_ad_urls = await ad_extractor.extract_own_ads_urls()
            LOG.info("%s found.", pluralize("ad", len(own_ad_urls)))

            if self.ads_selector == "all":  # download all of your adds
                LOG.info("Starting download of all ads...")

                success_count = 0
                # call download function for each ad page
                for add_url in own_ad_urls:
                    ad_id = ad_extractor.extract_ad_id_from_ad_url(add_url)
                    if await ad_extractor.navigate_to_ad_page(add_url):
                        await ad_extractor.download_ad(ad_id)
                        success_count += 1
                LOG.info("%d of %d ads were downloaded from your profile.", success_count, len(own_ad_urls))

            elif self.ads_selector == "new":  # download only unsaved ads
                # check which ads already saved
                saved_ad_ids = []
                ads = self.load_ads(ignore_inactive = False, exclude_ads_with_id = False)  # do not skip because of existing IDs
                for ad in ads:
                    ad_id = int(ad[2]["id"])
                    saved_ad_ids.append(ad_id)

                # determine ad IDs from links
                ad_id_by_url = {url:ad_extractor.extract_ad_id_from_ad_url(url) for url in own_ad_urls}

                LOG.info("Starting download of not yet downloaded ads...")
                new_count = 0
                for ad_url, ad_id in ad_id_by_url.items():
                    # check if ad with ID already saved
                    if ad_id in saved_ad_ids:
                        LOG.info("The ad with id %d has already been saved.", ad_id)
                        continue

                    if await ad_extractor.navigate_to_ad_page(ad_url):
                        await ad_extractor.download_ad(ad_id)
                        new_count += 1
                LOG.info("%s were downloaded from your profile.", pluralize("new ad", new_count))

        elif re.compile(r"\d+[,\d+]*").search(self.ads_selector):  # download ad(s) with specific id(s)
            ids = [int(n) for n in self.ads_selector.split(",")]
            LOG.info("Starting download of ad(s) with the id(s):")
            LOG.info(" | ".join([str(ad_id) for ad_id in ids]))

            for ad_id in ids:  # call download routine for every id
                exists = await ad_extractor.navigate_to_ad_page(ad_id)
                if exists:
                    await ad_extractor.download_ad(ad_id)
                    LOG.info("Downloaded ad with id %d", ad_id)
                else:
                    LOG.error("The page with the id %d does not exist!", ad_id)

    def __get_description(self, ad_cfg:Ad, *, with_affixes:bool) -> str:
        """Get the ad description optionally with prefix and suffix applied.

        Precedence (highest to lowest):
        1. Direct ad-level affixes (description_prefix/suffix)
        2. Global flattened affixes (ad_defaults.description_prefix/suffix)
        3. Legacy global nested affixes (ad_defaults.description.prefix/suffix)

        Args:
            ad_cfg: The ad configuration dictionary

        Returns:
            The raw or complete description with prefix and suffix applied
        """
        # Get the main description text
        description_text = ""
        if ad_cfg.description:
            description_text = ad_cfg.description

        if with_affixes:
            # Get prefix with precedence
            prefix = (
                # 1. Direct ad-level prefix
                ad_cfg.description_prefix if ad_cfg.description_prefix is not None
                # 2. Global prefix from config
                else self.config.ad_defaults.description_prefix
                or ""  # Default to empty string if all sources are None
            )

            # Get suffix with precedence
            suffix = (
                # 1. Direct ad-level suffix
                ad_cfg.description_suffix if ad_cfg.description_suffix is not None
                # 2. Global suffix from config
                else self.config.ad_defaults.description_suffix
                or ""  # Default to empty string if all sources are None
            )

            # Combine the parts and replace @ with (at)
            final_description = str(prefix) + str(description_text) + str(suffix)
            final_description = final_description.replace("@", "(at)")
        else:
            final_description = description_text

        # Validate length
        ensure(len(final_description) <= MAX_DESCRIPTION_LENGTH,
               f"Length of ad description including prefix and suffix exceeds {MAX_DESCRIPTION_LENGTH} chars. "
               f"Description length: {len(final_description)} chars.")

        return final_description

    def update_content_hashes(self, ads:list[tuple[str, Ad, dict[str, Any]]]) -> None:
        count = 0

        for (ad_file, ad_cfg, ad_cfg_orig) in ads:
            LOG.info("Processing %s/%s: '%s' from [%s]...", count + 1, len(ads), ad_cfg.title, ad_file)
            ad_cfg.update_content_hash()
            if ad_cfg.content_hash != ad_cfg_orig["content_hash"]:
                count += 1
                ad_cfg_orig["content_hash"] = ad_cfg.content_hash
                dicts.save_dict(ad_file, ad_cfg_orig)

        LOG.info("############################################")
        LOG.info("DONE: Updated [content_hash] in %s", pluralize("ad", count))
        LOG.info("############################################")

#############################
# main entry point
#############################


def main(args:list[str]) -> None:
    if "version" not in args:
        print(textwrap.dedent(rf"""
         _    _      _                           _                       _           _
        | | _| | ___(_)_ __   __ _ _ __  _______(_) __ _  ___ _ __      | |__   ___ | |_
        | |/ / |/ _ \ | '_ \ / _` | '_ \|_  / _ \ |/ _` |/ _ \ '_ \ ____| '_ \ / _ \| __|
        |   <| |  __/ | | | | (_| | | | |/ /  __/ | (_| |  __/ | | |____| |_) | (_) | |_
        |_|\_\_|\___|_|_| |_|\__,_|_| |_/___\___|_|\__, |\___|_| |_|    |_.__/ \___/ \__|
                                                   |___/
                                 https://github.com/Second-Hand-Friends/kleinanzeigen-bot
                                 Version: {__version__}
        """)[1:], flush = True)  # [1:] removes the first empty blank line

    loggers.configure_console_logging()

    signal.signal(signal.SIGINT, error_handlers.on_sigint)  # capture CTRL+C

    # sys.excepthook = error_handlers.on_exception
    # -> commented out because it causes PyInstaller to log "[PYI-28040:ERROR] Failed to execute script '__main__' due to unhandled exception!",
    #    despite the exceptions being properly processed by our custom error_handlers.on_exception callback.
    #    We now handle exceptions explicitly using a top-level try/except block.

    atexit.register(loggers.flush_all_handlers)

    try:
        bot = KleinanzeigenBot()
        atexit.register(bot.close_browser_session)
        nodriver.loop().run_until_complete(bot.run(args))
    except CaptchaEncountered as ex:
        raise ex
    except Exception:
        error_handlers.on_exception(*sys.exc_info())


if __name__ == "__main__":
    loggers.configure_console_logging()
    LOG.error("Direct execution not supported. Use 'pdm run app'")
    sys.exit(1)
