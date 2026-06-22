"""Shared reads of dataset sample statistics (the ANALYZE ``SAMPLE`` index).

``ANALYZE DATASET`` builds a sample of a dataset and records it as a special
``SAMPLE`` index in ``Metadata.Index``. That record carries the cost-based
optimizer's view of the dataset's scale: ``SourceCardinality`` (estimated row
count), ``SourceAvgItemSize`` (estimated average document size in bytes), and the
``SampleCardinalityTarget`` (how many rows the sample aims to hold). The presence
of the record is itself the signal that the dataset has been analyzed; its
absence means the optimizer is planning that dataset without statistics.

Two tools reason about this: ``get_dataset_statistics`` reports one dataset's
scale and freshness, and ``recommend_indexes`` flags when its advice rests on an
un-analyzed dataset (the native ``ADVISE`` advisor and the CBO cost indexes
without statistics, so the advice is lower-confidence there).

Read-only by construction: the only statement issued is a ``SELECT`` over the
metadata catalog. The gateway never runs ``ANALYZE`` — that is a write, left to a
human or operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cc_client import CCClient
from .errors import GatewayError

# All statements below are static literals filtered on the catalog's SAMPLE index
# structure; user-supplied dataverse/dataset names are bound as $parameters, never
# spliced into the text.
_DATASET_STATS_QUERY = (
    "SELECT i.DataverseName AS dataverse, i.DatasetName AS dataset, "
    "i.SourceCardinality AS rowCount, i.SourceAvgItemSize AS avgItemSize, "
    "i.SampleCardinalityTarget AS sampleTarget FROM Metadata.`Index` i "
    "WHERE i.IndexStructure = 'SAMPLE' "
    "AND i.DataverseName = $dv AND i.DatasetName = $ds LIMIT 1;"
)
_ANALYZED_DATASETS_QUERY = (
    "SELECT i.DataverseName AS dataverse, i.DatasetName AS dataset "
    "FROM Metadata.`Index` i WHERE i.IndexStructure = 'SAMPLE'"
)


@dataclass(frozen=True)
class DatasetStats:
    """The optimizer's sampled view of one dataset's scale.

    ``row_count`` and ``avg_item_size_bytes`` are the engine's estimates from the
    last ``ANALYZE DATASET``; ``estimated_size_bytes`` is their product, a
    convenience the agent would otherwise compute by hand.
    """

    dataverse: str | None
    dataset: str | None
    row_count: int
    avg_item_size_bytes: int
    sample_target: int

    @property
    def estimated_size_bytes(self) -> int:
        """Best-effort on-disk-equivalent size: rows times average item size."""
        return self.row_count * self.avg_item_size_bytes

    def to_dict(self) -> dict[str, Any]:
        """Serialize the scale estimates an agent reads to size a query."""
        return {
            "rowCountEstimate": self.row_count,
            "avgItemSizeBytes": self.avg_item_size_bytes,
            "estimatedSizeBytes": self.estimated_size_bytes,
            "sampleTarget": self.sample_target,
        }


def _int(value: Any) -> int:
    """Coerce a metadata numeric to int, defaulting to 0 for a missing/odd value."""
    return value if isinstance(value, int) else 0


def parse_sample_row(row: Any) -> DatasetStats | None:
    """Turn one projected ``SAMPLE``-index row into DatasetStats, or None if unusable."""
    if not isinstance(row, dict):
        return None
    return DatasetStats(
        dataverse=row.get("dataverse"),
        dataset=row.get("dataset"),
        row_count=_int(row.get("rowCount")),
        avg_item_size_bytes=_int(row.get("avgItemSize")),
        sample_target=_int(row.get("sampleTarget")),
    )


async def fetch_dataset_stats(
    client: CCClient, ccid: str, *, dataverse: str, dataset: str
) -> DatasetStats | None:
    """Fetch the sample statistics for one dataset.

    Returns None when the dataset has no ``SAMPLE`` index (never analyzed) or on a
    transport/query failure, so a caller treats "no statistics" as a safe default
    rather than aborting.
    """
    try:
        envelope = await client.execute(
            _DATASET_STATS_QUERY,
            client_context_id=ccid,
            statement_parameters={"dv": dataverse, "ds": dataset},
        )
    except GatewayError:
        return None
    for row in envelope.get("results") or []:
        parsed = parse_sample_row(row)
        if parsed is not None:
            return parsed
    return None


async def fetch_analyzed_datasets(
    client: CCClient, ccid: str, *, dataverse: str | None = None
) -> set[tuple[str, str]]:
    """Return the ``(dataverse, dataset)`` pairs that carry a ``SAMPLE`` index.

    A pair in the set has been analyzed and so has optimizer statistics. Scoped to
    one dataverse when given. Degrades to an empty set on failure, so a caller
    treats every dataset as un-analyzed rather than aborting.
    """
    statement = _ANALYZED_DATASETS_QUERY
    parameters: dict[str, Any] | None = None
    if dataverse is not None:
        statement += " AND i.DataverseName = $dv"
        parameters = {"dv": dataverse}
    statement += ";"
    try:
        envelope = await client.execute(
            statement, client_context_id=ccid, statement_parameters=parameters
        )
    except GatewayError:
        return set()
    analyzed: set[tuple[str, str]] = set()
    for row in envelope.get("results") or []:
        if isinstance(row, dict):
            dv, ds = row.get("dataverse"), row.get("dataset")
            if isinstance(dv, str) and isinstance(ds, str):
                analyzed.add((dv, ds))
    return analyzed
