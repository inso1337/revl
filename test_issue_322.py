"""Test for issue #322: async required-op acquisition in provide-method effect.

An async required-service op used as a method-body acquisition should be
refused (design 131 says method-time effects are untouched), not compiled to
invalid Python with `await` inside a sync generator.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError


def test_async_acquisition_in_provide_method_effect_refused():
    """Async acquisition in provide-method effect should raise EmitError."""
    src = """
service Db { fn q(sql: Str) -> Int  async fn aq(sql: Str) -> Int }
service Cache { async fn set(key: Str) }
component C requires db: Db provides cache: Cache {
  provide cache { async fn set(key) { effect db.aq(key) undo db.q(key) } }
}
"""
    # Should compile in the frontend (design 131 allows async provide methods)
    ir = compile_source(src, "t.rvl")
    assert ir  # Frontend accepted it
    
    # But Python backend should refuse at emission
    from backends.python.emit import emit
    with pytest.raises(Exception) as excinfo:
        emit(ir)
    
    assert "async acquisition" in str(excinfo.value).lower() or "await" in str(excinfo.value).lower()


def test_async_acquisition_in_provide_method_let_effect_refused():
    """Async acquisition in provide-method let-effect should raise EmitError."""
    src = """
service Db { fn q(sql: Str) -> Int  async fn aq(sql: Str) -> Int }
service Cache { async fn set(key: Str) }
component C requires db: Db provides cache: Cache {
  provide cache { async fn set(key) { let result = effect db.aq(key) undo db.q(key)  return 0 } }
}
"""
    # Should compile in the frontend
    ir = compile_source(src, "t.rvl")
    assert ir
    
    # But Python backend should refuse at emission
    from backends.python.emit import emit
    with pytest.raises(Exception) as excinfo:
        emit(ir)
    
    assert "async acquisition" in str(excinfo.value).lower() or "await" in str(excinfo.value).lower()


def test_sync_acquisition_in_provide_method_effect_works():
    """Sync acquisition in provide-method effect should work fine."""
    src = """
service Db { fn q(sql: Str) -> Int }
service Cache { fn set(key: Str) }
component C requires db: Db provides cache: Cache {
  provide cache { fn set(key) { effect db.q(key) undo db.q(key) } }
}
"""
    ir = compile_source(src, "t.rvl")
    assert ir
    
    # Python backend should emit successfully
    from backends.python.emit import emit
    output = emit(ir)
    assert output
    # Should not have async issues
    assert "await" not in output or "async def" in output  # await only in async functions


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
