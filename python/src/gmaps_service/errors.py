"""Errors raised by the maps service.

CAPTCHA and unusual-traffic responses are a hard stop. This package does not
call any CAPTCHA-solving service.
"""


class ScraperError(Exception):
    """Base error for scrape and API failures."""


class ConfigError(ScraperError):
    """Configuration is missing or invalid."""


class ParseError(ScraperError):
    """A saved or live payload could not be parsed."""


class PlacesAPIError(ScraperError):
    """The official Places API rejected a request."""


class CaptchaDetectedError(ScraperError):
    """Google showed a CAPTCHA or an unusual-traffic interstitial.

    Automated solving is intentionally unsupported. Headed runs can wait for a
    person to clear the page. Headless and service runs stop the job.
    """


CAPTCHA_MESSAGE = (
    "Google showed a CAPTCHA or unusual-traffic page. "
    "Automated CAPTCHA solving is not supported. "
    "In headed mode, solve it in the browser window. "
    "In headless or service mode this job stops. "
    "Retry later, use a proxy, or switch the backend to places with GOOGLE_PLACES_API_KEY."
)
