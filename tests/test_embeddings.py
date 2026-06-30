"""Tests for EmbeddingStore DB round-trip (index_file + search), with a fake embed_fn."""

from embeddings import EmbeddingStore


def _store(tmp_path):
    return EmbeddingStore(str(tmp_path / "emb.db"))


def _fake_embed(text):
    # Deterministic 3-d "embedding" from char categories so similar text scores higher.
    return [
        sum(c.isalpha() for c in text),
        sum(c.isdigit() for c in text),
        sum(c.isspace() for c in text),
    ]


class TestIndexAndSearch:
    def test_index_then_search_finds_chunk(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("alpha beta gamma\n" * 3)
        store = _store(tmp_path)
        store.index_file(str(f), _fake_embed)
        results = store.search(_fake_embed("alpha beta gamma"), top_k=5)
        assert results, "expected at least one indexed chunk"
        # result tuple: (score, file_path, chunk_text, start_line, end_line)
        assert results[0][1] == str(f)
        assert "alpha" in results[0][2]
        store.close()

    def test_reindex_replaces_old_chunks(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("first version\n")
        store = _store(tmp_path)
        store.index_file(str(f), _fake_embed)
        f.write_text("second version\n")
        store.index_file(str(f), _fake_embed)  # should DELETE old rows for this path
        rows = store.conn.execute("SELECT chunk_text FROM chunks WHERE file_path=?", (str(f),)).fetchall()
        assert len(rows) == 1 and "second" in rows[0][0]
        store.close()

    def test_blank_chunks_skipped(self, tmp_path):
        f = tmp_path / "blank.py"
        f.write_text("\n\n   \n")
        store = _store(tmp_path)
        store.index_file(str(f), _fake_embed)
        count = store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert count == 0
        store.close()

    def test_search_empty_store(self, tmp_path):
        store = _store(tmp_path)
        assert store.search([1, 2, 3]) == []
        store.close()

    def test_cosine_sim_edges(self):
        assert EmbeddingStore._cosine_sim([], []) == 0.0
        assert EmbeddingStore._cosine_sim([1, 2], [1, 2, 3]) == 0.0  # length mismatch
        assert EmbeddingStore._cosine_sim([0, 0], [1, 1]) == 0.0     # zero vector
        assert abs(EmbeddingStore._cosine_sim([1, 0], [1, 0]) - 1.0) < 1e-9
