You are conducting a manual UAT walkthrough of the Mahler operator console.
The console is running locally at: $url

Please perform this exact checklist using your browser tools:
1. Visit the home page (every view in `VIEWS` in `mahler/console/page.py` is accessible from here).
2. Test both phone and desktop layouts (resize your viewport or use device emulation).
3. Review a UAT row (Pass/Fail/bug-sheet on an item in waiting_for_owner or ended).
4. Answer the needs-you item.
5. Open the Capture form.
6. Toggle a backlog group.
7. Cycle the theme.
8. Perform at least one deliberately-repeated click on a button to verify it doesn't double-submit or fail silently.

Report anything that looks wrong in plain language (e.g. visual glue/overlap, a click with no visible effect, a broken link, layout breakage at phone width).
Rely on your human-like judgment. Do not assert against fixed expectations.

End your message with a pass/fail-style report.
