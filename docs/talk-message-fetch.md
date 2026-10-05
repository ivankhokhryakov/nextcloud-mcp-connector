# Reading a complete Talk message

`talk_browse` keeps message previews at 800 UTF-8 bytes so conversation history
remains bounded. Its pagination moves through messages, not through the body of
one message.

To retrieve a complete message, call `fetch` with its existing
`message:<conversation-token>:<message-id>` identifier. A single-message fetch
uses the existing 512 KiB fetched-text budget instead of the history preview
budget. This includes bodies with accents, emoji and newlines. The body is still
processed through the existing placeholder resolution, file exclusions and
server-marker sanitization; complete does not mean bypassing those protections.

Resolved bodies exceeding 512 KiB return an explicit error directing the caller
to Nextcloud Talk. They do not return an incomplete success, and this change
does not introduce body pagination. The budget applies to the resolved UTF-8
body, excluding the existing author line and result metadata.

The account-scoped conversation check, message selection and permitted message
types remain unchanged. No new tool, argument, authentication mode or permission
is introduced.

Regression coverage includes an ASCII body longer than the preview, multiline
Unicode, 32,000 four-byte characters, the exact byte boundary, oversized refusal,
and the registered MCP tool's structured response. Tests use synthetic messages
and mocked Nextcloud HTTP responses, not production conversation content.
