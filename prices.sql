-- Convert successive revisions to half-open intervals of knowledge before
-- filtering withdrawals or corrected effective dates.
WITH history AS (
    SELECT *, lead(recorded_at) OVER (
        PARTITION BY change_id ORDER BY revision
    ) AS superseded_at
    FROM revisions
), questions AS (
    SELECT *, 'at_order' AS perspective, placed_at AS known_at FROM orders
    UNION ALL
    SELECT *, 'at_audit', CAST(? AS TIMESTAMP) FROM orders
), candidates AS (
    SELECT q.*, h.change_id, h.revision, h.effective_at, h.recorded_at, h.unit_cents
    FROM questions AS q
    LEFT JOIN history AS h ON h.sku = q.sku
        AND h.recorded_at <= q.known_at
        AND (h.superseded_at IS NULL OR q.known_at < h.superseded_at)
        AND h.action = 'set'
        AND h.effective_at <= q.placed_at
    QUALIFY dense_rank() OVER (
        PARTITION BY q.order_id, q.perspective
        ORDER BY h.effective_at DESC NULLS LAST
    ) = 1
)
SELECT order_id, sku, placed_at, quantity, perspective,
       CASE count(change_id) WHEN 0 THEN 'missing' WHEN 1 THEN 'priced' ELSE 'ambiguous' END AS status,
       CASE WHEN count(change_id) = 1 THEN min(unit_cents) END AS price,
       list(struct_pack(change_id := change_id, revision := revision, unit_cents := unit_cents,
                        effective_at := effective_at, recorded_at := recorded_at)
            ORDER BY change_id) FILTER (WHERE change_id IS NOT NULL) AS sources
FROM candidates
GROUP BY order_id, sku, placed_at, quantity, perspective
ORDER BY order_id, perspective;
