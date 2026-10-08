import json

import httpx
import pytest

from ripstation.llm import LLM, Choice, extract_json
from ripstation.naming import (Namer, clean_label, strip_year, episode_path, movie_gate, movie_path,
                               season_disc_hints, tv_gate)
from ripstation.tmdb import TMDB, Candidate, Episode


def test_clean_label_and_hints():
    assert clean_label("THE_MATRIX_WS") == "The Matrix"
    assert clean_label("DEVIL_PRADA_PS") == "Devil Prada"  # real NAS disc: PS = pan & scan
    assert clean_label("ALIEN_UNRATED_EXTENDED_FF") == "Alien"
    assert season_disc_hints("FRIENDS_S2_D1") == (2, 1)
    assert season_disc_hints("SEINFELD_SEASON_3_DISC_2") == (3, 2)


def test_extract_json():
    assert extract_json('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("nope")


def test_movie_gate():
    cands = [Candidate(603, "movie", "The Matrix", 1999, runtime_min=136),
             Candidate(9999, "movie", "The Matrix Remix", 2003, runtime_min=90)]
    assert movie_gate(cands, 603, 136 * 60 + 10, 8) == []
    assert "runtime mismatch" in movie_gate(cands, 9999, 136 * 60, 8)[0]
    twin = cands + [Candidate(1, "movie", "The Matrix", 2030, runtime_min=134)]
    assert any("same title" in r for r in movie_gate(twin, 603, 136 * 60, 8))
    sequel = cands + [Candidate(604, "movie", "The Matrix Reloaded", 2003, runtime_min=138)]
    assert movie_gate(sequel, 603, 136 * 60, 8) == []


def test_tv_gate():
    eps = [Episode(n, f"Ep {n}", 22) for n in range(1, 25)]
    durs = [22 * 60] * 4
    assert tv_gate(eps, Choice(tmdb_id=1, kind="tv", season=2, first_episode=1), durs, None, 8) == []
    assert tv_gate(eps, Choice(tmdb_id=1, kind="tv", season=2, first_episode=5), durs, 5, 8) == []
    assert tv_gate(eps, Choice(tmdb_id=1, kind="tv", season=2, first_episode=9), durs, 5, 8)
    assert tv_gate(eps, Choice(tmdb_id=1, kind="tv", season=2, first_episode=23), durs, 23, 8)  # runs off the end


def test_paths():
    assert str(movie_path("Movies", "Alien: Resurrection", 1997)) == \
        "Movies/Alien Resurrection (1997)/Alien Resurrection (1997).mkv"
    assert str(episode_path("TV", "Friends", 2, 3, "The One with the List")) == \
        "TV/Friends/Season 02/Friends - S02E03 - The One with the List.mkv"


def fake_tmdb(movies=None, tv=None, seasons=None):
    movies, tv, seasons = movies or {}, tv or {}, seasons or {}

    def hit(q: str, title: str) -> bool:  # TMDB-like: every query word appears in the title
        return all(w in title.lower().split() for w in q.split())

    def handler(req: httpx.Request):
        p = req.url.path.removeprefix("/3")
        if p == "/search/movie":
            q = req.url.params["query"].lower()
            return httpx.Response(200, json={"results": [
                {"id": i, "title": m["title"], "release_date": m["date"], "overview": ""}
                for i, m in movies.items() if hit(q, m["title"])]})
        if p == "/search/tv":
            q = req.url.params["query"].lower()
            return httpx.Response(200, json={"results": [
                {"id": i, "name": s["name"], "first_air_date": s["date"]} for i, s in tv.items()
                if hit(q, s["name"])]})
        parts = p.strip("/").split("/")
        if parts[0] == "movie":
            m = movies[int(parts[1])]
            return httpx.Response(200, json={"title": m["title"], "release_date": m["date"], "runtime": m["runtime"]})
        if parts[0] == "tv" and len(parts) == 2:
            s = tv[int(parts[1])]
            return httpx.Response(200, json={"name": s["name"], "first_air_date": s["date"],
                                             "episode_run_time": [22], "seasons": [{"season_number": 1}, {"season_number": 2}]})
        if parts[0] == "tv" and parts[2] == "season":
            return httpx.Response(200, json={"episodes": seasons.get((int(parts[1]), int(parts[3])), [])})
        return httpx.Response(404)

    return TMDB("k", client=httpx.AsyncClient(base_url="https://api.themoviedb.org/3",
                                             transport=httpx.MockTransport(handler)))


def fake_llm(replies: list[dict | str]):
    it = iter(replies)

    def handler(req: httpx.Request):
        r = next(it)
        content = r if isinstance(r, str) else json.dumps(r)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return LLM("http://llm/v1", "m", client=httpx.AsyncClient(base_url="http://llm/v1",
                                                            transport=httpx.MockTransport(handler)))


MOVIES = {603: {"title": "The Matrix", "date": "1999-03-31", "runtime": 136},
          604: {"title": "The Matrix Reloaded", "date": "2003-05-15", "runtime": 138}}


async def test_identify_movie_auto_accept():
    llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"], "year_hint": None},
                    {"tmdb_id": 603, "kind": "movie", "reason": "runtime matches"}])
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("THE_MATRIX_WS", "movie", [8170])
    assert prop.choice["tmdb_id"] == 603 and prop.auto_accept, prop.gate_reasons


async def test_identify_rejects_hallucinated_id():
    llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"]},
                    {"tmdb_id": 42, "kind": "movie"}, {"tmdb_id": 42, "kind": "movie"}])
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("THE_MATRIX_WS", "movie", [8170])
    assert prop.choice is None and not prop.auto_accept


async def test_identify_wrong_runtime_goes_to_review():
    llm = fake_llm([{"kind": "movie", "search_queries": ["Matrix"]},
                    {"tmdb_id": 603, "kind": "movie"}])
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("MATRIX", "movie", [100 * 60])
    assert prop.choice["tmdb_id"] == 603 and not prop.auto_accept


async def test_identify_garbage_llm_output_falls_back():
    llm = fake_llm(["I think it's The Matrix!", "still no json", "nope", "nah"])
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("THE_MATRIX_WS", "movie", [8170])
    assert prop.candidates and not prop.auto_accept  # cleaned-label search still produced candidates


async def test_identify_tv_with_memory():
    tv = {1668: {"name": "Friends", "date": "1994-09-22"}}
    seasons = {(1668, 2): [{"episode_number": n, "name": f"E{n}", "runtime": 21 + n % 3} for n in range(1, 25)]}
    llm = fake_llm([{"kind": "tv", "search_queries": ["Friends"], "season_hint": 2, "disc_hint": 2},
                    {"tmdb_id": 1668, "kind": "tv", "season": 2, "first_episode": 5}])
    prop = await Namer(fake_tmdb(tv=tv, seasons=seasons), llm).identify(
        "FRIENDS_S2_D2", "tv", [1325, 1310, 1333, 1318], memory={"1668:2": 5})
    assert prop.auto_accept, prop.gate_reasons


async def test_llm_timeout_falls_back_to_review():
    def handler(req):
        raise httpx.ReadTimeout("slow")
    llm = LLM("http://llm/v1", "m", client=httpx.AsyncClient(base_url="http://llm/v1",
                                                            transport=httpx.MockTransport(handler)))
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("THE_MATRIX_WS", "movie", [8170])
    assert prop.candidates and not prop.auto_accept and "no usable search plan" in prop.gate_reasons[0]


def test_strip_year():
    assert strip_year("beetlejuice 1988") == ("beetlejuice", 1988)
    assert strip_year("Blade Runner (1982)") == ("Blade Runner", 1982)
    assert strip_year("2001 A Space Odyssey") == ("2001 A Space Odyssey", None)
    assert strip_year("Blade Runner 2049") == ("Blade Runner", 2049)  # ambiguous; label fallback still searches it
    assert strip_year("Alien") == ("Alien", None)


async def test_year_in_query_still_finds_movie():
    llm = fake_llm([{"kind": "movie", "search_queries": ["the matrix 1999"], "year_hint": 1999},
                    {"tmdb_id": 603, "kind": "movie"}])
    prop = await Namer(fake_tmdb(MOVIES), llm).identify("THE_MATRIX_WS", "movie", [8170])
    assert prop.auto_accept, prop.gate_reasons


def test_beta_key_parse_and_write(tmp_path):
    from ripstation.keys import parse_beta_key, write_key
    html = '<p>The current beta key is <code>T-' + 'a' * 60 + '</code> valid until end of October.</p>'
    key = parse_beta_key(html)
    assert key == "T-" + "a" * 60
    conf = tmp_path / "settings.conf"
    conf.write_text('app_DataDir = "/x"\napp_Key = "T-old"\n')
    write_key(key, conf)
    assert conf.read_text() == f'app_DataDir = "/x"\napp_Key = "{key}"\n'
    write_key("T-new", tmp_path / "fresh.conf")
    assert (tmp_path / "fresh.conf").read_text() == 'app_Key = "T-new"\n'


async def test_bad_llm_queries_rescued_by_label_fallback():
    """Real case from the NAS: the LLM searched for the wrong thing; the cleaned label still finds it."""
    movies = {**MOVIES, 350: {"title": "The Devil Wears Prada", "date": "2006-06-29", "runtime": 109},
              196204: {"title": "The Devil in Love", "date": "1966-01-01", "runtime": 97}}
    llm = fake_llm([{"kind": "movie", "search_queries": ["The Devil in Love"]},
                    {"tmdb_id": 350, "kind": "movie"}])
    prop = await Namer(fake_tmdb(movies), llm).identify("DEVIL_PRADA_PS", "movie", [6538])
    assert 350 in [c["tmdb_id"] for c in prop.candidates]
    assert prop.queries == ["The Devil in Love", "Devil Prada"]
    assert prop.auto_accept, prop.gate_reasons
