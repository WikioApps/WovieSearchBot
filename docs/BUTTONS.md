# Optional URL buttons

Up to four buttons, arranged in rows of two. Reusing a label replaces that button. Existing replies and captions remain unchanged until configured.

| Command | Effect |
|---|---|
| `/filterbutton hello \| Help \| https://example.com/help` | Add a static URL button to an existing local keyword filter. |
| `/filterbutton global hello \| Help \| https://example.com/help` | Add a global filter button; owner only. |
| `/filterbutton hello \| clear` | Remove this filter's buttons. |
| `/buttons 0 Details \| https://example.com/search?q={title}&year={year}` | Owner sets a global delivery button. |
| `/buttons Details \| https://example.com/search?q={title}` | Current group administrator sets a group delivery button. |
| `/buttons clear` | Clear the current group's list; an empty group list overrides the global list. |

Owners may provide an explicit chat ID after `/buttons`; group administrators may target only their current group. Group delivery lists apply to deliveries opened through that group's search links. Plain private searches use global delivery defaults.

Delivery URL fields: `{filename}` or `{file_name}`, `{title}`, `{year}`, `{quality}`, `{language}`, `{size}`. Values are percent-encoded individually. Put placeholders in the path, query or fragment; the HTTPS hostname must be fixed. No attribute access, format modifiers, embedded credentials, callback buttons or executable markup. Manual-filter URLs are static. Labels are 1–40 characters; configured URLs are at most 1024 characters. Invalid saved buttons are omitted rather than preventing file delivery.

Saving a filter again with `/filter` replaces its response and button list. Add the desired buttons afterward. Button text and destinations are administered configuration; avoid placing private media identifiers or secrets in third-party URLs.
