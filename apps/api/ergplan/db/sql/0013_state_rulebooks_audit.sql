-- Every write to a state rule book (generated, approved, rejected, superseded) is audited with its actor.
CREATE TRIGGER state_rulebooks_audit AFTER INSERT OR UPDATE OR DELETE ON state_rulebooks
  FOR EACH ROW EXECUTE FUNCTION ergplan_audit();
