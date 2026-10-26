"""What the game is doing right now, as opposed to what it contains.

Everything else in `wft/data` describes Warframe: which relic drops which part,
what a recipe needs, what is vaulted. None of that changes between sessions.
This package is the other kind of fact — the fissures that are open for the
next ninety minutes — and it is kept apart because it has to be treated
differently. It is never stored, it is never trusted after two minutes, and
nothing here is allowed to be a condition for anything the tool can already
answer offline.
"""
