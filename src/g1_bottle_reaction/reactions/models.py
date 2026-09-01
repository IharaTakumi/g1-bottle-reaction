from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class EncounterVariant:
    min_encounter_count: int
    speech: str


@dataclass(frozen=True, slots=True)
class Reaction:
    name: str
    motion: str
    speech: str
    speech_delay_seconds: float
    voice_profile: str = "neutral"
    priority: int = 0
    bypass_cooldown: bool = False
    encounter_variants: tuple[EncounterVariant, ...] = field(default_factory=tuple)

    def for_encounter(self, encounter_count: int) -> "Reaction":
        speech = self.speech
        for variant in sorted(
            self.encounter_variants, key=lambda item: item.min_encounter_count
        ):
            if encounter_count >= variant.min_encounter_count:
                speech = variant.speech
        return Reaction(
            name=self.name,
            motion=self.motion,
            speech=speech,
            speech_delay_seconds=self.speech_delay_seconds,
            voice_profile=self.voice_profile,
            priority=self.priority,
            bypass_cooldown=self.bypass_cooldown,
            encounter_variants=self.encounter_variants,
        )
