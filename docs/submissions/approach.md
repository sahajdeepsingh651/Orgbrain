# How will you build this? Technical / process approach

We build it as a gateway that sits in front of the LLM API. Developers point their existing
editor or agent at it with two environment variables, so nothing about how they work
changes.

On each request the gateway identifies the developer, screens the prompt for secrets and
personal data, searches the shared store for decisions that person is allowed to see, and
adds those to the request before forwarding it. Separately, a background worker reads
finished conversations, drafts structured decision records, checks them against existing
ones for duplicates and conflicts, and queues them for human approval before anything is
published to the organization.

The stack is deliberately thin: one Python service plus one background worker, and a single
PostgreSQL-family database with vector search that holds the records, their embeddings,
permissions and audit trail — no separate queue, cache or vector database. The embedding
model is open source and runs locally; Claude does the extraction, with schema-constrained
output so drafts are validated against the record structure before they are stored. The
review queue is a server-rendered internal page. Everything runs from a single compose file.

We build the end-to-end path first — requests routing through the gateway and one record
retrieved and injected into a real coding session — then the privacy screening and the
extraction pipeline, then the two-tool demonstration.
