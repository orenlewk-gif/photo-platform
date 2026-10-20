"""Unsubscribe from marketing emails (follow-ups, review requests, offers)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from html import escape as _html_escape

import main
from core.pages import render_page

router = APIRouter()


def _page(token: str, done: bool, email: str = "", status: int = 200) -> HTMLResponse:
    if not email:
        return HTMLResponse(render_page("unsubscribe.html", heading="This link isn't valid",
                                        message="Email us at info@bigskyphotos.com and we'll take you off our list.",
                                        form=""), status_code=404)
    if done:
        return HTMLResponse(render_page("unsubscribe.html", heading="You're unsubscribed",
                                        message=f"{_html_escape(email)} won't get any more offers or follow-ups from us. "
                                                "You'll still get receipts and download links for anything you buy.",
                                        form=""), status_code=status)
    form = (f'<form method="post" action="/unsubscribe/{_html_escape(token)}">'
            '<button type="submit">Unsubscribe</button></form>')
    return HTMLResponse(render_page("unsubscribe.html", heading="Unsubscribe from Crystal Images emails?",
                                    message=f"We'll stop sending offers and follow-ups to {_html_escape(email)}.",
                                    form=form))


@router.get("/unsubscribe/{token}")
def unsubscribe_page(token: str):
    """Asks first (mail scanners open links; a click on the button confirms)."""
    email = main._email_from_unsubscribe_token(token)
    return _page(token, done=bool(email and main._opted_out(email)), email=email or "")


@router.post("/unsubscribe/{token}")
async def unsubscribe(token: str, request: Request):
    """The button on the page, and the one-click unsubscribe from Gmail / Apple Mail (RFC 8058)."""
    email = main._email_from_unsubscribe_token(token)
    if email:
        main._record_optout(email, "link")
    return _page(token, done=True, email=email or "")
