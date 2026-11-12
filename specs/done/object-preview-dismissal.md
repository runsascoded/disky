# Dismissing the object preview

An opened object remains an inline drawer selected by `?open=`. Clicking outside the map and drawer now dismisses it. Pointer presses inside the drawer leave its controls usable; map presses may select another object without first closing the drawer. The explicit close button and Escape continue to clear the selection, preserving the selected scan and other view parameters. Escape in editable controls is left to that control, and secondary pointer presses do not dismiss the drawer.

The dismissal listeners live in `useObjectDismiss` and are removed when the drawer unmounts. The close control now reads **Close ×** with a larger target. No raw-object access or preview capability is enabled by this UI change.

Read-only production checks before the change confirmed that the close button and Escape already worked, while an outside click did nothing. Isolated Chrome checks of the new behavior cover outside dismissal, preserved drawer interaction, switching map objects, explicit close, Escape and mobile touch dismissal. The drawer fits a narrow viewport without internal overflow, and dismissal removes `open` while retaining the scan range.

The object drawer consumes Escape before the treemap's window listener interprets it as parent navigation. This also closes the drawer when a browser extension has merely prevented the key's default action. Browser checks assert that every dismissal preserves the viewed directory as well as the scan range.

Released on 2026-11-10 UTC to production Pages `5fa7e51c` and dev Pages `d3f36323`. Cloud Build `944fa8da-ad88-4057-bc5b-6f5eb2b8947d` passed 17 object-navigation baseline tests and the site type/build checks. The final Escape-consumption adjustment passed deployment type/build checks and isolated Chrome checks against production. All dismissal paths retained the directory and scan; clicks inside the drawer and switching map objects stayed active, with zero browser errors. The original eight-core validation was canceled while queued and retried on a standard builder.
