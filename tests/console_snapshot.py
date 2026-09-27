"""Aggregate separately rendered views for cross-view copy/markup assertions.

HTTP and fragment-selection tests use page.app/document directly.
"""
from mahler.console import page


def app(s):
    desktop = ''.join(page._settings_form(s["settings"], "desktop") if key == "settings"
                      else getattr(page, "_d_" + key)(s) for key, _ in page.VIEWS)
    phone = ''.join(page._settings_form(s["settings"], "phone") if key == "settings"
                    else getattr(page, "_p_" + key)(s) for key in page.TABS)
    return (page.app(s, view="now").replace(page._d_now(s), desktop, 1)
            + page._phone(s, "now").replace(page._p_now(s), phone, 1)
            + page._run_overlays(s) + page._revert_overlays(s) + page._bug_overlays(s)
            + page._capture_overlay(s) + page._release_preview_overlays(s))


def document(s):
    return page.document(s).replace(page.app(s), app(s), 1)
