-- All timestamps are integer UTC seconds. Both cutoffs are exclusive.
WITH RECURSIVE
signups AS (
    SELECT person, MIN(occurred_at) AS signed_at
    FROM events
    WHERE kind = 'signup' AND occurred_at < :through AND recorded_at < :as_of
    GROUP BY person
),
members AS (
    SELECT person, signed_at,
           unixepoch(signed_at, 'unixepoch', '-6 days', 'weekday 1', 'start of day') AS cohort
    FROM signups
),
sizes AS (
    SELECT cohort, COUNT(*) AS members FROM members GROUP BY cohort
),
active_weeks AS (
    SELECT DISTINCT m.cohort, m.person, (e.occurred_at - m.cohort) / 604800 AS week
    FROM members AS m
    JOIN events AS e ON e.person = m.person AND e.kind = 'active'
    WHERE e.occurred_at >= m.signed_at AND e.occurred_at < :through AND e.recorded_at < :as_of
      AND e.occurred_at < m.cohort + (:last_week + 1) * 604800
),
counts AS (
    SELECT cohort, week, COUNT(*) AS active FROM active_weeks GROUP BY cohort, week
),
offsets(week) AS (
    VALUES(0) UNION ALL SELECT week + 1 FROM offsets WHERE week < :last_week
)
SELECT date(s.cohort, 'unixepoch') AS cohort, o.week, s.members,
       CASE WHEN s.cohort + (o.week + 1) * 604800 <= :through
            THEN coalesce(c.active, 0) END AS active
FROM sizes AS s CROSS JOIN offsets AS o
LEFT JOIN counts AS c ON c.cohort = s.cohort AND c.week = o.week
ORDER BY s.cohort, o.week;
