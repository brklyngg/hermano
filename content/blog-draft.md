# I built the live voice interface I wanted: talk to your context

*A blog post + a freebie. The repo is at the bottom.*

> Companion to an earlier post on Karpathy's framing for finance teams. That one argues *why* context is the lever; this one shows what one feels like as a working product.

---

## The brainstorming partner I couldn't actually have

There's a kind of conversation I always wanted and could never really get.

The setup is something like this: I'm trying to learn something hard — a piece of physics behind something I'm building, an intricate piece of math I never bothered with in school, an architectural decision I can feel is wrong but can't yet say why. What I want is to talk it through with someone who actually knows the territory and who has the exact context for what I'm working on. Not a tutor. Not a Stack Overflow thread. A *brainstorming partner*.

The problem is that the right person for that conversation is, by definition,

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So the conversation never happens. Or it happens once, badly, and then I stop reaching out because I don't want to be the guy.

This was a real blocker for me — as a product builder, as a curious person, as a professional. I worked around it for years. Now I don't have to.

[GARY: optional half-paragraph — the "and now we have a solution" pivot. Keep it short.]

## Why I built it instead of using something off the shelf

I wrote earlier this week about [Karpathy's framing](LINK_TO_KARPATHY_FOR_FINANCE) — the context window is the lever, frontier intelligence is jagged, and the leverage compounds for the people who build the layer around the model rather than chasing whichever model is currently leading. If you haven't read that, the short version is: the engineering happens *before* the model sees a token, in the context layer.

This post is what that frame looks like when you turn it into a working product.

The off-the-shelf live-voice products are excellent at the *conversation* part. Low latency, natural turn-taking, barge-in. What none of them do is talk to *my context* — my notes, my agent's skills, my project memory, my Slack history, the residue of every other call I've had on the same problem. Without that, they're a smart stranger. With it, they're a colleague.

So I built the smaller half of the gap myself.

[GARY: optional bridge sentence here. The section can also land on "the smaller half of the gap myself" and move on.]

## The thing I built

The personal version is called Hermes Mini. The open-source version I'm releasing today is **Talk to Your Context**.

Architecturally it's a small stack, and the shape changed once I'd lived with it for a few weeks. The first cut funneled every substantive turn through a single slow `ask_agent` call. It worked, but it was wrong: most questions don't *need* the full agent loop, they need one specific piece of context delivered fast. So I split it.

- **Browser PWA ↔ OpenAI Realtime over WebRTC.** Handles barge-in, natural turn-taking, conversational feel. The Realtime model is the fast, shallow brain.
- **A dossier baked into every session.** When the call mints, the model's instructions already contain today's calendar, open loops, recent decisions, the people I've been talking to, and the working state from the last call. That's the difference between "generic until deep dive" and "knows what I'm working on from second 1."
- **A narrow toolkit, 50ms–1.5s.** Direct backends for the fast stuff: look up a specific card by ID, pull the calendar, search email, grep the notes vault, recall an excerpt from a prior call. No LLM in the path. The model picks the right one — often several in parallel — based on the dossier.
- **One slow path, `deep_research`.** Reserved for genuinely novel reasoning, drafting, or side-effecting work. Streams milestones back mid-call so the voice narrates progress instead of going dark. Brown noise covers the rare hard wait.
- **Memory sources, optional.** A small env-driven registry lets me wire the voice chat into my existing personal-assistant memory bank (user profile, shared facts, voice-specific learnings) without touching code. Empty by default for anyone cloning the repo.
- **A post-call learning loop.** The same agent call that updates the next session's dossier also extracts enduring voice-specific learnings — corrections, preferences, style notes — and a one-line summary into a recent-calls index. Including when the call ends abruptly (driving, signal drop). The thing learns, and never starts from scratch.
- **Text-mode fallback.** For when I want to type, or for long answers I'd rather read than hear.
- **Optional Slack archive.** Each call becomes a private Slack thread, searchable like any other channel.
- **Tailnet-only by default.** Loopback or Tailscale CIDR allowlist. Private by default. Not internet-exposed.

The defining design choice is the split between fast and slow paths and the dossier that makes the fast paths feel substantive. Most consumer voice products land the conversation but skip the context layer. That's the right tradeoff for them. It is the wrong tradeoff for me, because I'm not asking the live voice what the weather is. I'm asking it to think with me about the thing I'm in the middle of.

### One concrete moment: Tech Week prep

NYC Tech Week was the week I knew the architecture was earning its keep.

I drove into the city with a half-formed question — "what should I actually be doing for Tech Week?" — and got back, on the first turn, a ranked list of events tied to my actual GTM goals, not generic NYC startup advice. The dossier had already loaded my Crunchy Numbers positioning, the customer-acquisition wedge I'd committed to that week, my calendar (multiple accounts), and the recent decisions journal. One narrow `calendar` call reconciled what I was already booked into. One `gmail_search` surfaced the confirmation emails for the ticketed events. One `search_notes` pulled the strategy memo I'd written days earlier and forgotten about.

The output was a four-event plan with a clear rationale per event and a polite-skip suggestion for two more — phrased like a colleague who'd been on the prior planning call, because in a real sense it had.

Shallow voice would have given me a generic networking checklist. This gave me a schedule-aware plan tied to decisions I'd already made.

### Another moment: catching up on a half-remembered thread

A few days later I asked, while pacing the apartment, "Can you catch me up on that QuantumCT thing again?" — a thread I'd let go stale for two weeks.

The voice recovered the prior fractional-CFO conversation, corrected my drift (I'd half-remembered QuantumCT as a VC; it's actually a young Connecticut nonprofit/public-private quantum hub), then ran a `deep_research` call to layer in fresh public material. Halfway through the research it narrated, "still pulling — should have the funding-model breakdown in another minute" so I knew it hadn't stalled. The synthesis landed on a concrete wedge I hadn't seen before: a grant-readiness and restricted-funds diagnostic, which is *exactly* the kind of finance-ops work I'm strong at.

Shallow voice would have asked me to restate the backstory or hallucinated a generic venture-capital pitch. This grounded the entity correctly, used the prior conversation, and only reached for the slow path when it actually needed to.

### A third: a half-formed YC question on the train

"What should I say and do around YC and Tech Week?" — same kind of half-baked ask.

The model pulled my Crunchy Numbers/YC positioning decisions from the journal, scanned recent Gmail for accelerator and customer signals, reconciled the calendar against the target event list, and ran a brief `deep_research` to synthesize a focused set of asks for three specific events (Vertical AI in Accounting, a CFO breakfast, a finance-agent meetup). The output was three specific conversations to drive, not platitudes about networking.

That's the whole point. Half-baked question in; grounded plan out, in under thirty seconds for the fast bits and a bounded slow path for the part that actually needed synthesis.

## What works, and what doesn't

A few things I underestimated:

- **The transcript-feeds-back-into-memory loop matters more than the voice does.** Each call leaves a residue. After two weeks the agent feels less like a tool and more like a colleague who was on the last call too.
- **Barge-in is what makes it brainstorming, not lecture.** Being able to interrupt mid-sentence is the difference between a conversation and a podcast. I didn't realize how much that mattered until I had it.
- **The systems mindset transfers.** I'm a CPA-turned-builder. Internal controls, business systems, finance ops — that's the lane I came from. Building a context layer is the same kind of work: you're designing the machinery around something stochastic so it produces reliable output. Operators get this in their bones; engineers sometimes have to learn it.

What's still imperfect, said honestly:

- **`deep_research` calls lag.** Thirty seconds to a few minutes depending on the question. There's no way around this with current architectures — substantive synthesis takes real wall time. The mid-call milestone narration helps ("still pulling, should have the next section soon") but doesn't eliminate the friction. The fix was tighter triage — most questions don't need the slow path, the dossier and narrow toolkit cover them — but the rare hard one still costs real time. For the brainstorming use case, that's a price I'll pay.
- **Single-user.** Auth is local-first. Multi-user with proper isolation is a different product.
- **You bring your own backends.** The narrow toolkit ships as adapters — a default Supabase-shaped journal, a Google Workspace adapter, ripgrep over a notes directory, a transcript-recall backend. There's a bundled stub backend for the slow path so you can smoke-test the full stack in 60 seconds without standing anything up. Real answers need a real backend you wire in.
- **Memory is opt-in.** The voice chat can read an existing personal-memory bank if you point it at one (user profile, shared facts, voice-specific learnings) and writes back to a voice-owned learnings file post-call. All four paths default empty. Mine is wired into the agent I already run, which is why the voice chat feels like a continuation; for someone cloning the repo, it'll feel like a clean agent until they wire it.

## A note on agent harnesses

While I'm here: it has never been more tempting to build a crazy complex agent harness — multi-agent orchestration, sub-agents that hire sub-agents, a 47-step ReAct loop with tool-routing on every turn. **It's a trap.**

The right model is leaner. Hire agents the way you'd add new roles to a lean team. One agent with a good context layer and the right three skills will out-perform a baroque multi-agent harness on almost any real task — and you can actually debug it when it goes sideways. Talk to Your Context is small on purpose. Most of the complexity I cared about lives in the *context* layer (the LLM-wiki, the skills, the persistent memory) — not in the agent topology.

That distinction is the one I'd want a reader to take away. Context is leverage. Harnesses are mostly LARP-ing.

## Here's the repo

[`github.com/brklyngg/talk-to-your-context`](https://github.com/brklyngg/talk-to-your-context) — MIT, give-it-away.

There's a bundled in-process stub backend so you can run the full stack in 60 seconds with just an OpenAI API key. When you're ready for real answers, point `AGENT_API_BASE` at any OpenAI-compatible chat-completions SSE endpoint — your own agent, `ollama serve`, vLLM, whatever. The narrow toolkit ships with adapter stubs you replace with your own data sources. There are README stubs for swapping the Slack transcript archive to Telegram, Discord, Matrix, or email. If you build an adapter, send a PR.

I'm releasing it as a freebie because the tools that taught me to build were freebies. Open source is its own pay-it-forward economy and I'd like to be in it. If you make something better with this, I want to hear about it.

[GARY: closing line. Personal, short.]

---

*Notes for Gary on this re-cut (post-bifurcation):*
- The Karpathy thesis (jagged intelligence, car-wash, chess, "context window is the lever," 10x ceiling, "outsource thinking not understanding") has been removed from this post. It now lives in the **Karpathy for Finance Teams** post, which is assumed published before this one drops.
- Replaced the deleted "Why this works now" section with a one-paragraph bridge that links to that post (`LINK_TO_KARPATHY_FOR_FINANCE` placeholder — fill once that crunchy.tools URL exists).
- Removed the Karpathy quote from the LinkedIn close (the prereq post already used it; double-billing the same source on LinkedIn within a week dilutes both posts).
- Anti-harness section stays — it's product-philosophy adjacent, not Karpathy translation.
- Result: tighter post, sharper product focus, no double-billing on the thesis. Post is now ~1,000 words before your fills (was ~1,250). The shorter shape suits the product-release frame.
- Pull-quotes still used here: the "annoying expert" line and the harness-trap paraphrase. Both Karpathy quotes moved to the prereq post.
