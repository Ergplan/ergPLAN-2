-- Stage 14: a golden test may say which versions it is for (rulebook/golden.py `applies`): the
-- versions in force on a date (given.effective_on) and of one origin (given.origin). Two tariff
-- years of one DISCOM then each have their own cases. The approval gate counts the tests that
-- apply to the version being approved; it is otherwise unchanged: only an analyst, at least one
-- applicable test, every applicable test run on this version and passed on its latest run.
CREATE OR REPLACE FUNCTION ergplan_test_applies(given jsonb, eff_from date, eff_to date, origin text)
RETURNS boolean AS $$
  SELECT (given->>'effective_on' IS NULL
          OR ((given->>'effective_on')::date >= eff_from
              AND (eff_to IS NULL OR (given->>'effective_on')::date <= eff_to)))
     AND (given->>'origin' IS NULL OR given->>'origin' = origin)
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION ergplan_version_gate() RETURNS trigger AS $$
DECLARE
  role text := nullif(current_setting('ergplan.role', true), '');
  n_tests int; n_passed int;
BEGIN
  IF NEW.status = 'approved' AND OLD.status IS DISTINCT FROM 'approved' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'only a draft can be approved (was %)', OLD.status USING ERRCODE = 'check_violation';
    END IF;
    IF role IS DISTINCT FROM 'analyst' THEN
      RAISE EXCEPTION 'approval needs role analyst (invariant 3)' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT count(*) INTO n_tests FROM rule_tests t
      WHERE t.jurisdiction_id = NEW.jurisdiction_id AND t.family = NEW.family
        AND ergplan_test_applies(t.given, NEW.effective_from, NEW.effective_to, NEW.origin);
    SELECT count(DISTINCT r.test_id) INTO n_passed FROM rule_test_runs r
      JOIN rule_tests t ON t.id = r.test_id
      WHERE r.rulebook_version_id = NEW.id AND t.jurisdiction_id = NEW.jurisdiction_id AND t.family = NEW.family
        AND ergplan_test_applies(t.given, NEW.effective_from, NEW.effective_to, NEW.origin)
        AND r.passed AND NOT EXISTS (
          SELECT 1 FROM rule_test_runs r2 WHERE r2.rulebook_version_id = NEW.id AND r2.test_id = r.test_id
            AND r2.ran_at > r.ran_at);
    IF n_tests = 0 OR n_passed < n_tests THEN
      RAISE EXCEPTION 'approval refused: % of % golden tests passed on this version', n_passed, n_tests
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF OLD.status IN ('superseded', 'rejected') AND NEW.status <> OLD.status THEN
    RAISE EXCEPTION 'a % version is final', OLD.status USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
