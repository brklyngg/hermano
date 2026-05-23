# Talk to your context: the live voice interface I wanted

*A blog post, plus two open source projects you can clone today.*

> Companion to an earlier post on Karpathy's framing for finance teams. That one argues *why* context is the lever; this one is about what happens when you turn the context layer into something you can talk to.

---

## The brainstorming partner I couldn't actually have

There's a kind of conversation I always wanted and could never really get.

The setup is something like this: I'm trying to learn something hard — a piece of physics behind something I'm building, an intricate piece of math I never bothered with in school, an architectural decision I can feel is wrong but can't yet say why. What I want is to talk it through with someone who actually knows the territory and who has the exact context for what I'm working on. Not a tutor. Not a Stack Overflow thread. A *brainstorming partner*.

The problem is that the right person for that conversation is, by definition,

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So the conversation never happens. Or it happens once, badly, and then I stop reaching out because I don't want to be the guy.

This was a real blocker for me — as a product builder, as a curious person, as a professional. I worked around it for years. Now I don't have to.

[GARY: optional half-paragraph — the "and now we have a solution" pivot. Keep it short.]

## Why context is the whole game

I wrote earlier this week about [Karpathy's framing](LINK_TO_KARPATHY_FOR_FINANCE) — the context window is the lever, frontier intelligence is jagged, and the leverage compounds for the people who build the layer around the model rather than chasing whichever model is currently leading. If you haven't read that, the short version is: the engineering happens *before* the model sees a token, in the context layer.

This post is what that frame looks like when you turn the context layer into a conversation.

The off-the-shelf live-voice products are excellent at the *conversation* part. Low latency, natural turn-taking, barge-in. What none of them do is talk to *my context* — my notes, my agent's skills, my project memory, the residue of every other call I've had on the same problem. Without that, they're a smart stranger. With it, they're a colleague.

That's the gap worth closing. And it turns out a few people noticed at the same time.

## A category, not a product

I started building my version — **Hermano** — a couple of months ago, after Karpathy's talk crystallized what I'd been feeling. While I was iterating on it, [Garry Tan](https://github.com/garrytan) shipped his — **Mars** and **Venus**, two voice personas inside his agent brain project [gbrain](https://github.com/garrytan/gbrain) — within the last few days. Independently. Same week.

That's a kindred-spirit moment, not a competitive one. When two people who don't talk to each other build the same thing at the same time, the category is real. So this post isn't a product launch — it's a tour of the idea, followed by two open source options you can clone today.

## What it feels like when it works

A few concrete moments from the last couple of weeks.

### Tech Week prep

NYC Tech Week was the week I knew the architecture was earning its keep.

I drove into the city with a half-formed question — "what should I actually be doing for Tech Week?" — and got back, on the first turn, a ranked list of events tied to my actual GTM goals, not generic NYC startup advice. The voice already had my Crunchy Numbers positioning, the customer-acquisition wedge I'd committed to that week, my calendar across multiple accounts, and the recent decisions journal. One narrow calendar call reconciled what I was already booked into. One Gmail search surfaced the confirmation emails for the ticketed events. One notes search pulled the strategy memo I'd written days earlier and forgotten about.

The output was a four-event plan with a clear rationale per event and a polite-skip suggestion for two more — phrased like a colleague who'd been on the prior planning call, because in a real sense it had.

Shallow voice would have given me a generic networking checklist. This gave me a schedule-aware plan tied to decisions I'd already made.

### Catching up on a half-remembered thread

A few days later I asked, while pacing the apartment, "Can you catch me up on that QuantumCT thing again?" — a thread I'd let go stale for two weeks.

The voice recovered the prior fractional-CFO conversation, corrected my drift (I'd half-remembered QuantumCT as a VC; it's actually a young Connecticut nonprofit/public-private quantum hub), then ran a deeper research call to layer in fresh public material. Halfway through, it narrated, "still pulling — should have the funding-model breakdown in another minute" so I knew it hadn't stalled. The synthesis landed on a concrete wedge I hadn't seen before: a grant-readiness and restricted-funds diagnostic, which is *exactly* the kind of finance-ops work I'm strong at.

Shallow voice would have asked me to restate the backstory or hallucinated a generic venture-capital pitch. This grounded the entity correctly, used the prior conversation, and only reached for the slow path when it actually needed to.

### A half-formed YC question on the train

"What should I say and do around YC and Tech Week?" — same kind of half-baked ask.

The model pulled my Crunchy Numbers/YC positioning decisions from the journal, scanned recent Gmail for accelerator and customer signals, reconciled the calendar against the target event list, and ran a brief research call to synthesize a focused set of asks for three specific events. The output was three specific conversations to drive, not platitudes about networking.

That's the whole point. Half-baked question in; grounded plan out, in under thirty seconds for the fast bits and a bounded slow path for the part that actually needed synthesis.

## The shape of one of these things

The architecture isn't the post, but it's useful to know what's in the box if you're going to clone one. After living with mine for a few weeks, the design that earns its keep looks like this:

- **Browser PWA ↔ a realtime voice API over WebRTC.** Handles barge-in, natural turn-taking, conversational feel. The realtime model is the fast, shallow brain.
- **A dossier baked into every session.** When the call mints, the model's instructions already contain today's calendar, open loops, recent decisions, the people you've been talking to, and the working state from the last call. That's the difference between "generic until deep dive" and "knows what you're working on from second 1."
- **A narrow toolkit, 50ms–1.5s.** Direct backends for the fast stuff — look up a specific card by ID, pull the calendar, search email, grep the notes vault, recall an excerpt from a prior call. No LLM in the path. The model picks the right one based on the dossier.
- **One slow path.** Reserved for genuinely novel reasoning, drafting, or side-effecting work. Streams milestones back mid-call so the voice narrates progress instead of going dark.
- **A post-call learning loop.** Each call leaves a residue. Decisions, commitments, open questions, corrections, preferences — extracted and merged into the next session's dossier. Including when the call ends abruptly. The thing learns, and never starts from scratch.

The defining choice is the split between fast and slow paths, and the dossier that makes the fast paths feel substantive. Most consumer voice products land the conversation but skip the context layer. That's the right tradeoff for them. It is the wrong tradeoff for me, because I'm not asking the live voice what the weather is. I'm asking it to think with me about the thing I'm in the middle of.

## Two open source options to play with

Both projects do the same job — let you have a real conversation with your actual working context — but they make different bets. Pick the one that fits how you already work.

### Hermano

[`github.com/brklyngg/hermano`](https://github.com/brklyngg/hermano) — MIT.

This is mine. Bet: **voice-side context engineering over a small agent backend.** The work goes into the layer between the model and your stuff — the dossier baked into every session, three-way cross-call memory, the slow path with narrated progress, continuity through the 60-minute realtime cap, post-call extraction that runs even when the call ends abruptly. The backends are pluggable: Mission Control / Supabase, Google Workspace, ripgrep over a notes directory, transcript recall. There's a bundled stub backend so you can run the full stack in 60 seconds with just an OpenAI key. Strongest if you want the felt experience of "a colleague who was on the prior call."

### Mars + Venus (in gbrain)

[`github.com/garrytan/gbrain`](https://github.com/garrytan/gbrain) — recipe at `recipes/agent-voice`.

Garry's. Bet: **a packaged persona layer over a serious brain.** The voice surface is intentionally minimal — two named personas (Mars, the introspective thought partner; Venus, the fast EA) and a read-only tool router — sitting on top of gbrain, which is a heavy, opinionated knowledge graph (typed relations, hybrid search, hundreds of thousands of pages). It installs as a copy-into-your-repo recipe with tests, a PII guard, and an end-to-end voice roundtrip — a more mature install story than mine starts with. Strongest if you already live inside a structured personal brain, or if you want the persona/mode split out of the box.

The short version of the difference: Mars and Venus have the better costume; Hermano has the better workflow skeleton. Garry's voice layer is not a clean replacement for mine, and mine isn't a clean replacement for his. They're two answers to the same prompt, and there's a lot I plan to steal from his packaging.

## What works, and what doesn't

A few things I underestimated:

- **The transcript-feeds-back-into-memory loop matters more than the voice does.** Each call leaves a residue. After two weeks the agent feels less like a tool and more like a colleague who was on the last call too.
- **Barge-in is what makes it brainstorming, not lecture.** Being able to interrupt mid-sentence is the difference between a conversation and a podcast. I didn't realize how much that mattered until I had it.
- **The systems mindset transfers.** I'm a CPA-turned-builder. Internal controls, business systems, finance ops — that's the lane I came from. Building a context layer is the same kind of work: you're designing the machinery around something stochastic so it produces reliable output. Operators get this in their bones; engineers sometimes have to learn it.

What's still imperfect, said honestly:

- **The slow path is slow.** Thirty seconds to a few minutes for genuine synthesis. Mid-call narration helps but doesn't eliminate the friction. The fix was tighter triage — most questions don't need the slow path — but the rare hard one still costs real time.
- **Single-user.** Both options here are local-first. Multi-user with proper isolation is a different product.
- **You bring your own backends.** Hermano ships adapter stubs you replace with your own data sources. Mars and Venus route through gbrain by default, so the full effect lights up when there's a brain underneath. A bundled stub gets either stack running in a minute, but real answers need a real backend you wire in.

## A note on agent harnesses

While I'm here: it has never been more tempting to build a crazy complex agent harness — multi-agent orchestration, sub-agents that hire sub-agents, a 47-step ReAct loop with tool-routing on every turn. **It's a trap.**

The right model is leaner. Hire agents the way you'd add new roles to a lean team. One agent with a good context layer and the right three skills will out-perform a baroque multi-agent harness on almost any real task — and you can actually debug it when it goes sideways. Both Hermano and Mars/Venus are small on purpose. Most of the complexity that matters lives in the *context* layer (the wiki, the skills, the persistent memory) — not in the agent topology.

That distinction is the one I'd want a reader to take away. Context is leverage. Harnesses are mostly LARP-ing.

## Grab one and tinker

Both repos are MIT-ish and built for cloning. If you want the colleague-who-was-on-the-prior-call feel and you're comfortable wiring backends, start with Hermano. If you already live inside a structured personal brain or you want the persona split as a starting point, start with Mars and Venus.

[GARY: closing line. Personal, short.]

---

<section className="not-prose mt-12 rounded-2xl border border-stone-200 bg-stone-50 px-5 py-5 text-sm text-stone-700 shadow-sm">
  <h3 className="mb-2 text-base font-semibold text-stone-900">Repos and tools referenced in this post</h3>
  <div className="grid gap-2 sm:grid-cols-2">
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://github.com/brklyngg/hermano">Hermano</a>
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://github.com/garrytan/gbrain">gbrain (Mars + Venus voice recipe)</a>
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://platform.openai.com/docs/guides/realtime">OpenAI Realtime API</a>
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://github.com/supabase/supabase">Supabase</a>
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://obsidian.md/">Obsidian</a>
    <a className="text-blue-600 underline underline-offset-2 hover:text-blue-800" target="_blank" rel="noopener noreferrer" href="https://github.com/BurntSushi/ripgrep">ripgrep</a>
  </div>
</section>

---

*Drafting notes for Gary on this re-cut:*
- Shifted from "Hermano product launch" to "the category is real, here are two ways in" per Gary's direction.
- Treats Garry Tan's same-week release of Mars/Venus as parallel-discovery validation, not competition. Admiring, not defensive.
- Comparison framing borrowed from Hermes' analysis: Garry's is "a packaged persona layer over a brain"; Hermano is "a voice-side context layer over an agent." Both real, different bets.
- Reused the three concrete moments verbatim (Tech Week, QuantumCT, YC) and the anti-harness section — those are the post's load-bearing prose.
- Removed the "I built it" architectural list framing; replaced with "the shape of one of these things" as descriptive of the category.
- Karpathy thesis still lives in the prereq post; this one bridges to it once.
- Voice gate: ran `gary-voice-calibration`. No hype, no AI-isms. Admiring of Garry without swooning. CPA-to-builder angle preserved.
- Open: closing line + the optional Karpathy-bridge half-paragraph are still Gary-fills.
- Length: ~1,150 words before fills (was ~1,250 in the prior cut). Tighter, with the second option doing some of the work the launch framing used to do alone.
