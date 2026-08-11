"""LLM analysis layer over the code graph.

Reads the graph graph_core builds; never writes edges to it. The only graph
mutations are `summary_*` / `reaches_sink` / `from_entry` properties on Function
nodes, plus `:AnalysisDismissal` nodes for dismissal memory.

    from analysis import pipeline
    result = pipeline.run(store, repo="myrepo", root="/path/to/src")

STAGE ORDER IS LOAD-BEARING

  A. file_pass               One LLM call per file. Incremental by body_hash. Also
                          writes the scalar signals every later stage ranks on —
                          nothing below it can see more than it reported.
  R. reach.mark_all()     Reachability to a fixpoint, both directions. No depth
                          bound — it is a closure, not an enumeration.
  P. paths                Enumerate INSIDE the universe R produced. Bounded, hubs
                          excluded, trusted edges only.
  B. path_pass               Judge paths from summaries alone. No source sent.
  D. adversarial_pass               Adversarial panel tries to REFUTE what survived.
  F. findings.report()    Dedupe, apply prior dismissals, rank.

Each stage narrows the set, so the expensive passes only see what the cheap ones
could not settle. Every pass builds its LLM client lazily, so a fully-cached
re-run needs no provider SDK at all.

DEPENDENCIES (both optional, imported lazily)
    pip install "anthropic[bedrock]"   # Anthropic models on Bedrock
    pip install boto3                  # Nova, via the Converse API

TWO THINGS THAT ARE NOT DONE
  * Nova reasoning config is UNVERIFIED — see llm.NovaBedrock. It defaults to
    sending none rather than guessing a key.
  * The sink catalog covers DB and reflection only. exec / file_write /
    deserialize / response kinds still need adding to graph_core.external_api
    before reach.DANGEROUS_KINDS can seed from them.
"""
