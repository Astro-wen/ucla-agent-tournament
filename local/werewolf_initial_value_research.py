from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy


class InitialValuePolicy(WerewolfPolicy):
    def __init__(self, rank=5):
        if type(rank) is not int or not 0 <= rank < 6:
            raise ValueError('initial investigation rank must be0through5')
        super().__init__()
        self.rank = rank

    def _investigate(self, v, targets):
        pool = sorted(set(targets) & set(v.others_alive))
        if (v.role == 'seer' and v.day == 1 and len(v.alive) == 7 and len(pool) == 6
                and not v.investigations and not v.messages and not v.votes):
            return pool[self.rank], 'fixed first-investigation rank selected from development terminal outcomes'
        return super()._investigate(v, targets)


class InitialValueContestant(Contestant):
    def __init__(self, rank=5):
        self.initial_rank = rank
        super().__init__()
        self.ww = InitialValuePolicy(rank)

    def _fresh(self):
        super()._fresh()
        self.ww = InitialValuePolicy(self.initial_rank)


def rank0():
    return InitialValueContestant(0)


def rank1():
    return InitialValueContestant(1)


def rank2():
    return InitialValueContestant(2)


def rank3():
    return InitialValueContestant(3)


def rank4():
    return InitialValueContestant(4)


def rank5():
    return InitialValueContestant(5)
