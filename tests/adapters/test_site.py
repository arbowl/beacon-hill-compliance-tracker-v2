"""Tests for page normalization."""

from pathlib import Path

from beacon.adapters.site import normalized_hash

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"


def weather(day: str, icon: str, temp: str) -> bytes:
    return (
        f'<div class="visible-lg-inline weatherBox">\n'
        f'<span class="weatherDate">{day}</span>\n'
        f'<img id="wicon" class="weatherIcon" src="https://x/{icon}.png">\n'
        f'<span class="weatherDivider"> &#124; </span>\n'
        f'<span class="weatherStats">{temp}&deg;F</span>\n</div>'
    ).encode()


def test_noise_ignored_content_not():
    a = b"<p>x</p>" + weather("November 18, 2026", "03n", "57")
    b = b"<p>x</p>" + weather("November 19, 2026", "02d", "41")
    token = b'<input name="__RequestVerificationToken" type="hidden" value="abc">'
    assert normalized_hash(a) == normalized_hash(b) == normalized_hash(b + token)
    assert normalized_hash(a) != normalized_hash(b"<p>y</p>" + weather("x", "y", "1"))


def test_real_page_weather_box_removed():
    body = (PAGES / "H67.html").read_bytes()
    altered = body.replace(b"November 18, 2026", b"November 19, 2026").replace(
        b"02n.png", b"02d.png"
    )
    assert altered != body
    assert normalized_hash(altered) == normalized_hash(body)
