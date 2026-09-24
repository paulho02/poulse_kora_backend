"""HTML (and matching plain-text) bodies for the mail this backend sends.

Kept apart from app/core/email.py, which is about *connectors* — how a message
reaches a provider — and from app/core/email_verification.py, which is about
codes and Redis. This module is only about what the message looks like.

Three constraints shape the markup, and all three are unusual enough to be worth
stating, because the natural instinct is to write a normal web page:

- **Tables and inline styles, no stylesheet.** Email clients are not browsers.
  Gmail strips `<style>` blocks in some contexts, Outlook renders through Word's
  engine, and neither flexbox nor grid can be relied on. A centered table with
  inline `style` attributes is the one layout that survives all of them, which is
  why this looks like markup from 2005.
- **No external assets.** No webfonts (they silently fall back), and no images —
  most clients block remote images by default, so a logo image would render as a
  broken box for a first-time recipient, and the code itself must never be one:
  it has to be selectable text so it can be copied, and readable by a screen
  reader.
- **Every HTML mail carries a plain-text alternative.** Some clients are
  configured to prefer text, and a `multipart/alternative` with only an HTML part
  reads as a spam signal. `verification_email` therefore returns both and both
  say the same thing.

Colors track the Flutter app's palette (`lib/src/core/theme/app_colors.dart`):
emerald as the single accent, neutral greys for everything else, so the mail and
the app look like the same product.

Copy is localized, per the rule in CLAUDE.md that user-facing backend strings are
not hardcoded English prose. This does not use the `api_error` code contract,
because that contract hands a *code* to the client and lets the client's `.arb`
supply the words — there is no client here to do that, so the words have to live
on this side. That makes it the same situation as app/core/banner.py, and it
follows the same shape: one dict per locale, resolved server-side against the
locale the request carried.
"""

from html import escape

from app.core.config import settings

# --- brand ---------------------------------------------------------------
# Emerald accent from AppColors.accentLight; the rest is a neutral grey ramp,
# deliberately not tinted with the accent (the app kills that tint too).
_ACCENT = "#059669"
_INK = "#111827"
_BODY_TEXT = "#374151"
_MUTED = "#6B7280"
_HAIRLINE = "#E5E7EB"
_CARD = "#FFFFFF"
_CANVAS = "#F3F4F6"
_CODE_BG = "#ECFDF5"

# A system font stack rather than a webfont: an `@font-face` in mail either gets
# stripped or fails to load, and the fallback is what most recipients would see
# anyway. Better to pick the fallback deliberately.
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "'Helvetica Neue',Arial,sans-serif"
)
# Separate stack for the code itself. A monospace face makes 0/O and 1/l/I
# distinguishable, which matters when someone is retyping six digits by hand.
_MONO_FONT = "'SF Mono',SFMono-Regular,Menlo,Consolas,'Courier New',monospace"


_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "verify_subject": "Your Peerkola verification code",
        "verify_heading": "Verify your email",
        "verify_lead": (
            "Enter this code in the app to finish setting up your account."
        ),
        "verify_expiry_one": "This code expires in 1 minute.",
        "verify_expiry_other": "This code expires in {minutes} minutes.",
        "verify_ignore": (
            "If you didn't create a Peerkola account, you can ignore this "
            "email — nothing will happen."
        ),
        "footer_automated": "This is an automated message, so please don't reply.",
    },
    "de": {
        "verify_subject": "Dein Peerkola Bestätigungscode",
        "verify_heading": "Bestätige deine E-Mail-Adresse",
        "verify_lead": (
            "Gib diesen Code in der App ein, um dein Konto fertig einzurichten."
        ),
        "verify_expiry_one": "Dieser Code läuft in 1 Minute ab.",
        "verify_expiry_other": "Dieser Code läuft in {minutes} Minuten ab.",
        "verify_ignore": (
            "Falls du kein Peerkola Konto erstellt hast, kannst du diese "
            "E-Mail ignorieren — es passiert nichts."
        ),
        "footer_automated": (
            "Das ist eine automatische Nachricht, bitte antworte nicht darauf."
        ),
    },
}


def _t(locale: str, key: str, **params: object) -> str:
    """One string, resolved to `locale` with a fall back to DEFAULT_LOCALE.

    Falling back per *key* rather than per locale means a half-translated locale
    degrades to a single English sentence among translated ones, instead of
    raising KeyError and failing the send outright. A verification code that
    arrives in mixed languages is worth more than one that never arrives.
    """
    table = _STRINGS.get(locale, {})
    template = table.get(key) or _STRINGS[settings.DEFAULT_LOCALE][key]
    return template.format(**params) if params else template


def _layout(
    *,
    locale: str,
    heading: str,
    lead: str,
    code: str,
    note: str,
    footer_lines: list[str],
) -> str:
    """The one HTML shell every mail here shares.

    Structure is three nested tables: a full-width canvas that centers things in
    clients ignoring `margin: auto`, a 600px card (the width that fits the
    average desktop preview pane without horizontal scroll on a phone), and the
    content itself.
    """
    footer_html = "\n".join(
        f'            <p style="margin:0 0 8px 0;">{escape(line)}</p>'
        for line in footer_lines
    )
    return f"""\
<!doctype html>
<html lang="{escape(locale)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{escape(heading)}</title>
</head>
<body style="margin:0;padding:0;background-color:{_CANVAS};">
<!-- Preheader: the grey line a client shows next to the subject. Left empty it
     scrapes whatever text comes first, which is usually the wordmark. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
{escape(lead)}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background-color:{_CANVAS};">
  <tr>
    <td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             border="0" style="width:100%;max-width:600px;">
        <tr>
          <td align="center" style="padding-bottom:20px;font-family:{_FONT};
                     font-size:15px;font-weight:600;letter-spacing:0.3px;
                     color:{_INK};">
            Peer<span style="color:{_ACCENT};">kola</span>
          </td>
        </tr>
        <tr>
          <td style="background-color:{_CARD};border:1px solid {_HAIRLINE};
                     border-radius:12px;padding:36px 32px;">
            <h1 style="margin:0 0 12px 0;font-family:{_FONT};font-size:22px;
                       line-height:1.3;font-weight:600;color:{_INK};">
              {escape(heading)}
            </h1>
            <p style="margin:0 0 28px 0;font-family:{_FONT};font-size:15px;
                      line-height:1.6;color:{_BODY_TEXT};">
              {escape(lead)}
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                   border="0">
              <tr>
                <td align="center" style="background-color:{_CODE_BG};
                           border:1px solid {_ACCENT};border-radius:10px;
                           padding:20px 12px;font-family:{_MONO_FONT};
                           font-size:32px;font-weight:700;letter-spacing:8px;
                           color:{_ACCENT};">
                  {escape(code)}
                </td>
              </tr>
            </table>
            <p style="margin:20px 0 0 0;font-family:{_FONT};font-size:14px;
                      line-height:1.6;color:{_MUTED};">
              {escape(note)}
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 8px 0 8px;font-family:{_FONT};font-size:12px;
                     line-height:1.6;color:{_MUTED};">
{footer_html}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


def verification_email(code: str, locale: str) -> tuple[str, str, str]:
    """`(subject, text, html)` for the email-verification code."""
    minutes = settings.EMAIL_VERIFICATION_CODE_TTL_SECONDS // 60
    key = "verify_expiry_one" if minutes == 1 else "verify_expiry_other"
    expiry = _t(locale, key, minutes=minutes)

    subject = _t(locale, "verify_subject")
    heading = _t(locale, "verify_heading")
    lead = _t(locale, "verify_lead")
    ignore = _t(locale, "verify_ignore")
    footer = _t(locale, "footer_automated")

    # The code stands alone on its own line so a mail client's "copy" gesture and
    # a phone's tap-to-select both grab it cleanly.
    text = f"{heading}\n\n{lead}\n\n{code}\n\n{expiry}\n\n{ignore}\n\n{footer}\n"
    html = _layout(
        locale=locale,
        heading=heading,
        lead=lead,
        code=code,
        note=expiry,
        footer_lines=[ignore, footer],
    )
    return subject, text, html
