# Reddit post — CFactory (cockpit / observability over autonomous agents)

Target subreddits: r/devops, r/programming, r/kubernetes

---

## Title

We built a control tower over an autonomous coding pipeline, and the most useful feature is that the tests refuse to lie

## Body

We run a four-service pipeline that takes a GitHub issue and produces a tested pull request unattended: one service plans, one builds the code inside an ephemeral Kubernetes Job, one generates and runs tests, and a fourth — the cockpit — is the single pane of glass over all three.

The thing we keep coming back to is not the code generation. It is observability. When there is no human writing the code, the usual CI dashboard is not enough. You need to see what the machine is actually doing while it does it. So the cockpit threads one task through plan, build, and test as a single correlation and shows:

- a live pipeline strip with plan/build/test counts moving as the run progresses
- an event feed of the run's real completions (actual emitted events, not a synthetic progress bar)
- live agent terminals streamed in while each agent works, parallel agents on one board
- a human-review queue for the runs that need a decision
- billing-aware token and cost per task, with the model tier each stage used stamped into its completion event

The honest part is what sold us on it. In a recent run the tester built a `slugify` helper that compiled and looked fine, then failed one of twelve test verdicts on a unicode edge case. The verification gate capped it at the lowest assurance level and auto-filed a handback instead of certifying it. A green checkbox is impossible unless a real test runner actually executed. On the board that shows up as "stopped short, needs a fix," not a false success.

Same run also surfaced a real gap in our own tooling: the verdict is computed correctly but its auto-post back to the PR is gated by a fix we are now tracking as an issue. We would rather the cockpit name that than imply a polish the run does not have.

Technical writeup with the pipeline strip and the honesty-gate detail: [blog link]

Happy to talk about the Kubernetes Job-per-task model, the correlation design, or how the verification levels are computed.

## Short FAQ

**Is this just CI with extra steps?**
No — CI runs tests you wrote. Here the pipeline writes the code and the tests, and the cockpit's job is to make that watchable and to stop it from overclaiming. The verification gate recomputes an assurance level from the actual signals (coverage, stability, mutation, semantic relevance, CI parity) rather than trusting a pass.

**Why a Kubernetes Job per task?**
Each build runs in its own throwaway Job refreshed to the current main tip, opens its own PR, then the Job is gone. No shared mutable build host.

**What happens when a build fails?**
It surfaces as failed on the board rather than green. A build that quietly produced no code is reported as a failure, and a build with a failing test is capped at the lowest assurance level with an auto-filed handback.

**Is it self-hosted?**
Yes. Local-first by default, one operator, one store. Multi-tenant is behind a flag.
