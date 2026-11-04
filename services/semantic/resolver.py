"""Natural language -> MetricRef.

This is the accuracy-critical path and it is deliberately framed as a
**retrieval** problem rather than a generation problem. Retrieval can be
measured, tuned, and — crucially — made to fail loudly: a score below threshold
produces a clarifying question instead of a confident guess.

Asking "did you mean blended or paid CAC?" is what a senior analyst does. A
junior one guesses. Guessing is how you lose the account, so the thresholds
here are tuned to ask rather than assume.

The embedding provider is optional. With no embeddings the resolver runs
lexically and still works, which keeps the eval suite runnable offline and in
CI without an API key (BLUEPRINT 8.5).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .models import Metric, SemanticModel

#: Score floor for binding a metric without asking the user.
CONFIDENT_SCORE = 0.55
#: The top candidate must also beat the runner-up by this margin. Two metrics
#: scoring 0.8 and 0.79 is precisely the case where guessing is worst.
CONFIDENT_MARGIN = 0.12
#: Minimum summed IDF of matched terms before a non-phrase match may bind.
#: Tuned so a single common word ("rate", "cost") is not enough on its own but
#: one distinctive word ("refund", "impressions") is.
MIN_IDF_MASS = 2.0

_STOPWORDS = frozenset(
    """a an and are as at be been being but by can could did do does for from
    had has have how i in into is it its me my of on or our ours should show
    so tell than that the their them there these they this those to us was we
    were what when where which who whom why will with would you your""".split()
)

#: Words that carry no metric signal: time expressions, quantities, and the verbs
#: people wrap questions in. An unmatched filler word says nothing about whether we
#: understood the question; an unmatched *domain* word says a great deal.
_FILLER = frozenset(
    """many much number numbers figure figures total overall trend trends
    show give tell get got see look looking doing going
    today yesterday week weeks month months quarter quarters year years
    day days date dates last past previous current recent latest ago
    january february march april may june july august september october
    november december q1 q2 q3 q4 ytd mtd wtd
    now then still just about across over under between
    good bad high low up down""".split()
)

_SYNONYM_HINTS: dict[str, tuple[str, ...]] = {
    # Everyday phrasings that no metric author thinks to list.
    "money": ("revenue", "sales"),
    "topline": ("revenue",),
    "top": ("revenue",),
    "line": ("revenue",),
    "earn": ("revenue",),
    "earned": ("revenue",),
    "make": ("revenue",),
    "made": ("revenue",),
    "making": ("revenue",),
    "bringing": ("revenue",),
    "turnover": ("revenue",),
    "sold": ("revenue", "sales"),
    "selling": ("revenue", "sales"),
    "spend": ("spend", "cost"),
    "spent": ("spend", "cost"),
    "cost": ("cost", "spend"),
    "customers": ("customers",),
    "buyers": ("customers",),
    "signups": ("customers",),
    "basket": ("order", "value"),
    "aov": ("average", "order", "value"),
    "cac": ("customer", "acquisition", "cost"),
    "cpa": ("customer", "acquisition", "cost"),
    "roas": ("return", "ad", "spend"),
    "ctr": ("click", "through", "rate"),
}


class EmbeddingProvider(Protocol):
    """Swappable: a real model in production, absent in CI."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class Candidate:
    metric: Metric
    score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    reasons: list[str] = field(default_factory=list)
    idf_mass: float = 0.0
    #: True when the query named this metric outright — an exact phrase hit or
    #: full name-token coverage. Strong enough to override the unknown-term gate.
    phrase_matched: bool = False
    matched_phrase: str = ""

    @property
    def key(self) -> str:
        return self.metric.key


@dataclass
class Resolution:
    """Either a confident binding or an explicit request for clarification.

    `needs_clarification` is a first-class outcome, not an error. The question
    text is built here because the resolver is the only component that knows
    *why* it was unsure.
    """

    query: str
    candidates: list[Candidate]
    metric: Metric | None = None
    needs_clarification: bool = False
    question: str | None = None

    @property
    def confidence(self) -> float:
        return self.candidates[0].score if self.candidates else 0.0

    @property
    def margin(self) -> float:
        if len(self.candidates) < 2:
            return 1.0
        return self.candidates[0].score - self.candidates[1].score

    def top_keys(self, n: int = 5) -> list[str]:
        return [c.key for c in self.candidates[:n]]


def _stem(word: str) -> str:
    """Crude suffix stripping, enough to match 'refund' against 'refunds'.

    A real stemmer is overkill for a corpus of sixty metric names, and the
    aggressive ones ('acquisition' -> 'acquisit') hurt more than they help here.
    """
    for suffix in ("ies", "es", "s", "ed", "ing"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords dropped, everyday phrasings expanded.

    Each token is emitted alongside its stem so singular/plural and verb/noun
    phrasings match ('how much did we refund' vs the metric named 'refunds').
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    out: list[str] = []
    for word in words:
        if word in _STOPWORDS:
            continue
        out.append(word)
        stem = _stem(word)
        if stem != word:
            out.append(stem)
        out.extend(_SYNONYM_HINTS.get(word, ()))
    return out


class BM25Index:
    """Small, dependency-free BM25. The corpus is dozens of metrics, not millions.

    Pulling in a search engine for 60 documents would be the kind of
    infrastructure decision this project's blueprint spends a whole section
    arguing against.
    """

    K1 = 1.4
    B = 0.75

    def __init__(self, documents: dict[str, str]) -> None:
        self.doc_tokens = {key: tokenize(text) for key, text in documents.items()}
        self.doc_len = {key: len(toks) for key, toks in self.doc_tokens.items()}
        self.avg_len = (sum(self.doc_len.values()) / len(self.doc_len)) if self.doc_len else 0.0
        self.freqs = {key: Counter(toks) for key, toks in self.doc_tokens.items()}

        self.doc_freq: Counter[str] = Counter()
        for toks in self.doc_tokens.values():
            for term in set(toks):
                self.doc_freq[term] += 1
        self.n_docs = len(self.doc_tokens)

    def idf(self, term: str) -> float:
        df = self.doc_freq.get(term, 0)
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def score(self, query: str) -> dict[str, float]:
        return {key: total for key, (total, _) in self.score_detailed(query).items()}

    def score_detailed(self, query: str) -> dict[str, tuple[float, float]]:
        """Return (bm25, idf_mass) per document.

        `idf_mass` is the summed IDF of the query terms that actually matched.
        It answers a different question from the BM25 score: not "how well does
        this rank" but "how much *distinguishing* evidence supports it at all".
        A query matching only a word like 'rate' or 'cost', which half the
        catalogue contains, has a high rank and almost no idf_mass — and that is
        exactly the case where binding a metric is a false positive.
        """
        terms = tokenize(query)
        scores: dict[str, tuple[float, float]] = {}
        for key, freq in self.freqs.items():
            total = 0.0
            idf_mass = 0.0
            for term in set(terms):
                tf = freq.get(term, 0)
                if not tf:
                    continue
                idf = self.idf(term)
                denominator = tf + self.K1 * (
                    1 - self.B + self.B * (self.doc_len[key] / (self.avg_len or 1))
                )
                total += idf * (tf * (self.K1 + 1)) / denominator
                idf_mass += idf
            if total:
                scores[key] = (total, idf_mass)
        return scores


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _rrf(ranks: Sequence[int | None], k: int = 60) -> float:
    """Reciprocal rank fusion — combines rankings without calibrating scores.

    BM25 scores and cosine similarities live on incomparable scales; fusing by
    rank sidesteps the normalisation problem entirely.
    """
    return sum(1.0 / (k + rank) for rank in ranks if rank is not None)


class MetricResolver:
    def __init__(
        self,
        model: SemanticModel,
        *,
        embeddings: EmbeddingProvider | None = None,
        usage_counts: dict[str, int] | None = None,
    ) -> None:
        self.model = model
        self.embeddings = embeddings
        self.usage_counts = usage_counts or {}

        self._index = BM25Index({m.key: m.search_text for m in model.metrics})
        self._vectors: dict[str, list[float]] | None = None
        if embeddings is not None:
            keys = [m.key for m in model.metrics]
            texts = [m.search_text for m in model.metrics]
            self._vectors = dict(zip(keys, embeddings.embed(texts), strict=True))

    # ---- scoring ------------------------------------------------------

    def rank(self, query: str, top_n: int = 5) -> list[Candidate]:
        query = _measure_phrase(query)
        detailed = self._index.score_detailed(query)
        lexical = {key: score for key, (score, _) in detailed.items()}
        idf_mass = {key: mass for key, (_, mass) in detailed.items()}
        lexical_order = sorted(lexical, key=lambda k: lexical[k], reverse=True)
        lexical_rank = {key: i + 1 for i, key in enumerate(lexical_order)}

        semantic_rank: dict[str, int] = {}
        if self._vectors and self.embeddings is not None:
            query_vector = self.embeddings.embed([query])[0]
            similarity = {k: _cosine(query_vector, v) for k, v in self._vectors.items()}
            order = sorted(similarity, key=lambda k: similarity[k], reverse=True)
            semantic_rank = {key: i + 1 for i, key in enumerate(order)}

        candidates: list[Candidate] = []
        for metric in self.model.metrics:
            lr = lexical_rank.get(metric.key)
            sr = semantic_rank.get(metric.key)
            if lr is None and sr is None:
                continue

            fused = _rrf([lr, sr])
            reasons: list[str] = []

            # Exact-name and exact-synonym hits are not fuzzy matching; treat them
            # as near-certain rather than letting BM25 average them away.
            matched, phrase = _longest_phrase_match(query, _phrases(metric))
            if matched:
                # Scale the bonus by how specific the matched phrase is. Without
                # this, "return on ad spend" matches ROAS's full synonym *and*
                # marketing_spend's "ad spend" for the same bonus, and the two tie.
                # The longer phrase is the more specific claim, so it wins.
                fused += 0.30 + 0.09 * len(phrase.split())
                reasons.append(f"matched '{phrase}'")
            elif _name_tokens_covered(metric, query):
                # Order-independent full-name coverage: "how much did we spend on
                # marketing" contains every token of "marketing spend" but not in
                # that order, so phrase matching misses it while the intent is
                # unambiguous. Weighted close to a phrase match deliberately.
                fused += 0.33
                matched = True
                reasons.append("all name tokens present")

            if metric.certified:
                fused += 0.05
                reasons.append("certified")
            used = self.usage_counts.get(metric.key, 0)
            if used:
                fused += min(0.04, 0.01 * math.log1p(used))
                reasons.append(f"used {used}x before")

            candidates.append(
                Candidate(
                    metric=metric,
                    score=fused,
                    lexical_rank=lr,
                    semantic_rank=sr,
                    reasons=reasons,
                    idf_mass=idf_mass.get(metric.key, 0.0),
                    # Named rather than `phrase_matched` because full name-token
                    # coverage counts as strong evidence too, even out of order.
                    phrase_matched=matched,
                    matched_phrase=phrase,
                )
            )

        candidates.sort(key=lambda c: (-c.score, c.key))
        return self._normalize(candidates)[:top_n]

    @staticmethod
    def _normalize(candidates: list[Candidate]) -> list[Candidate]:
        """Map fused scores into 0..1 so thresholds mean something stable."""
        if not candidates:
            return candidates
        top = candidates[0].score or 1.0
        for candidate in candidates:
            candidate.score = round(min(candidate.score / top, 1.0), 4)
        return candidates

    # ---- resolution ----------------------------------------------------

    def unknown_terms(self, query: str) -> set[str]:
        """Content words in the question that appear nowhere in the catalogue.

        Filler — time words, quantity words, verbs of asking — is excluded, so
        what remains is genuinely domain vocabulary the semantic layer has never
        heard of. That is the strongest available signal that the right answer is
        "I don't have that" rather than the nearest fuzzy match.
        """
        vocabulary = set(self._index.doc_freq)
        terms = {t for t in tokenize(query) if t not in _FILLER}
        return {t for t in terms if t not in vocabulary and _stem(t) not in vocabulary}

    def _has_substantive_match(self, query: str) -> bool:
        """At least one matched term must be real domain vocabulary, not filler."""
        vocabulary = set(self._index.doc_freq)
        for term in tokenize(_measure_phrase(query)):
            if term in _FILLER:
                continue
            if term in vocabulary or _stem(term) in vocabulary:
                return True
        return False

    def resolve(self, query: str, top_n: int = 5) -> Resolution:
        candidates = self.rank(query, top_n=top_n)

        if not candidates:
            return Resolution(
                query=query,
                candidates=[],
                needs_clarification=True,
                question=(
                    "I don't have a metric that matches that. Defined metrics are: "
                    + ", ".join(sorted(m.display for m in self.model.metrics)[:12])
                    + ". Would you like me to define a new one?"
                ),
            )

        best = candidates[0]
        margin = best.score - candidates[1].score if len(candidates) > 1 else 1.0

        # If the question contains a domain word that appears nowhere in the whole
        # catalogue, the user is asking about something we have not defined.
        # "what is our conversion rate" matches CTR on the word "rate" alone while
        # "conversion" matches nothing — answering with CTR would be confidently
        # wrong about a metric this company does not have. Saying so is the answer.
        # "show me the numbers" matches order_count's synonym "number of orders",
        # but every word that matched is filler. Filler is not evidence of intent,
        # so there is nothing here to bind to.
        if not self._has_substantive_match(query):
            return Resolution(
                query=query,
                candidates=candidates,
                needs_clarification=True,
                question=(
                    "Which metric would you like? I have "
                    + ", ".join(sorted(m.display for m in self.model.metrics if m.certified)[:8])
                    + "."
                ),
            )

        unknown = self.unknown_terms(query)
        if unknown and not best.phrase_matched:
            return Resolution(
                query=query,
                candidates=candidates,
                needs_clarification=True,
                question=(
                    f"I don't have a metric for '{' '.join(sorted(unknown))}'. The closest "
                    f"defined metrics are {', '.join(c.metric.display for c in candidates[:3])}. "
                    "Want me to define a new metric instead?"
                ),
            )

        if not best.phrase_matched and best.idf_mass < MIN_IDF_MASS:
            return Resolution(
                query=query,
                candidates=candidates,
                needs_clarification=True,
                question=(
                    "I don't have a metric that clearly matches that. The closest defined "
                    f"metrics are {', '.join(c.metric.display for c in candidates[:3])}, but none "
                    "of them is really what you asked for. Want me to define a new metric?"
                ),
            )

        # Normalisation pins the top score at 1.0, so the decision rests on the
        # margin: the real question is whether anything else is nearly as good.
        if margin >= CONFIDENT_MARGIN:
            return Resolution(query=query, candidates=candidates, metric=best.metric)

        tied = [c for c in candidates if best.score - c.score < CONFIDENT_MARGIN][:3]
        options = "; ".join(f"{c.metric.display} ({_describe(c.metric)})" for c in tied)
        return Resolution(
            query=query,
            candidates=candidates,
            needs_clarification=True,
            question=f"Did you mean {options}?",
        )

    def resolve_dimension(
        self, query: str, top_n: int = 3, *, include_time: bool = False
    ) -> list[tuple[str, float]]:
        """Rank dimensions the same way, for 'by channel' / 'per region' phrasing.

        Time columns are excluded by default. They are registered as dimensions so
        they can be filtered on, but you do not group "by spend_date" — you set a
        grain. Leaving them in meant "ad spend by channel" ranked `spend_date`
        first, purely because the query contains the word "spend".
        """
        documents = {
            f"{entity.key}.{dim.key}": " ".join(
                [dim.key.replace("_", " "), dim.display, *dim.synonyms]
            )
            for entity in self.model.entities
            for dim in entity.dimensions
            if include_time or not dim.is_time
        }
        scores = BM25Index(documents).score(_grouping_phrase(query))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]
        top = ranked[0][1] if ranked else 1.0
        return [(key, round(score / top, 4)) for key, score in ranked]


def _name_tokens_covered(metric: Metric, query: str) -> bool:
    """True when the query contains every token of some name this metric answers to."""
    query_stems = {_stem(t) for t in re.findall(r"[a-z0-9]+", query.lower())}
    if not query_stems:
        return False
    for phrase in _phrases(metric):
        tokens = {_stem(t) for t in re.findall(r"[a-z0-9]+", phrase)}
        if len(tokens) > 1 and tokens <= query_stems:
            return True
    return False


def _phrases(metric: Metric) -> list[str]:
    """Every name this metric legitimately answers to."""
    names = {metric.key.replace("_", " ").lower()}
    if metric.label:
        names.add(metric.label.lower())
    names |= {s.lower() for s in metric.synonyms}
    return [n for n in names if n]


_GROUPING_KEYWORD = re.compile(
    r"\b(?:split\s+by|broken\s+down\s+by|grouped\s+by|group\s+by|by|per|across)\b",
    re.IGNORECASE,
)


def _grouping_split(query: str) -> tuple[str, str]:
    """Split a question into (measure phrase, grouping phrase).

    Splitting on the *last* grouping keyword, not the first. "cost per customer
    by channel" contains two: "per" belongs to the metric's own name and "by"
    introduces the grouping. Taking the first match leaves the measure as bare
    "cost", which is ambiguous between marketing spend and CAC — the exact
    confusion this function exists to prevent.
    """
    matches = list(_GROUPING_KEYWORD.finditer(query))
    if not matches:
        return query, ""
    last = matches[-1]
    head = query[: last.start()].strip()
    tail = query[last.end() :].strip()
    if not head or not tail:
        return query, tail or query
    return head, tail


def _measure_phrase(query: str) -> str:
    """The part of the question that names the *metric*, with the grouping removed.

    "sales by acquisition channel" is a question about sales. Leaving the tail in
    lets `new_customers` match on "acquisition" and tie with `gross_revenue`,
    producing a clarifying question about something that was never ambiguous.
    """
    return _grouping_split(query)[0]


def _grouping_phrase(query: str) -> str:
    """Just the part of the question that names the grouping.

    Scoring dimensions against the whole question lets the measure's own words
    bleed in: "acquisitions by geo" ranked `channel` first, because `channel`
    carries the synonym "acquisition channel". Only the text after "by"/"per"
    describes how to split, so only that text is scored.
    """
    return _grouping_split(query)[1] or query


def _stem_phrase(text: str) -> str:
    return " ".join(_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()))


def _longest_phrase_match(query: str, phrases: Sequence[str]) -> tuple[bool, str]:
    """Longest whole-word phrase match, or (False, '').

    Word boundaries matter: without them 'sales' matches inside 'net sales' and
    gross_revenue ties net_revenue on a query that unambiguously means the latter.

    Both sides are stemmed, so "how much did we refund" matches the metric named
    "refunds". Without that, the only support for `refunds` is a BM25 term it
    shares with net_revenue's description, and the two tie into a pointless
    clarifying question.
    """
    haystack = _stem_phrase(query)
    best = ""
    for phrase in phrases:
        normalized = _stem_phrase(phrase)
        if not normalized:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", haystack) and (
            len(normalized.split()) > len(best.split())
            or (len(normalized.split()) == len(best.split()) and len(normalized) > len(best))
        ):
            best = normalized
    return bool(best), best


def _describe(metric: Metric) -> str:
    if metric.description:
        text = " ".join(metric.description.split())
        return text[:80] + ("..." if len(text) > 80 else "")
    if metric.kind == "ratio":
        return f"{metric.numerator} / {metric.denominator}"
    if metric.kind == "derived":
        return str(metric.expression)
    return f"{metric.agg.value if metric.agg else ''} of {metric.sql}"
