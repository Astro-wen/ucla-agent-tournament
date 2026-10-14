from local.werewolf_evolution_research import BASE, EvolutionContestant, frozen_genome


def vote_only():
    genome = list(BASE)
    genome[9:12] = frozen_genome()[9:12]
    return EvolutionContestant(genome)


def night_only():
    genome = list(BASE)
    genome[12:15] = frozen_genome()[12:15]
    return EvolutionContestant(genome)


def combined():
    return EvolutionContestant(frozen_genome())
