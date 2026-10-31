"""Economics from a region pack, for any crop.

Prices are per kg of the marketed product (fresh tubers, grain at trade
moisture); WOFOST yields are dry matter, so each crop carries the dry-matter
fraction of its marketed form. The virtual farm's truth is anchored to the real
field, so yields are valued as simulated (``farm_factor`` 1.0).
"""
from __future__ import annotations

from dataclasses import dataclass

from fa_planner.weekly import PotatoEconomics

#: dry-matter fraction of the marketed product
DRY_MATTER = {"potato": 0.20, "maize": 0.845, "soybean": 0.87, "rice": 0.86, "wheat": 0.865,
              "barley": 0.865, "sugarbeet": 0.23, "sorghum": 0.86, "cotton": 0.92}


@dataclass(frozen=True)
class Economics:
    currency: str
    crop: str
    price_per_kg: float
    dry_matter: float
    fungicide_product_ha: float
    herbicide_product_ha: float
    drone_service_ha: float
    scan_ha: float
    irrigation_cm_ha: float
    assumed: bool
    region: str

    @classmethod
    def from_region(cls, pack, crop: str) -> "Economics":
        c = pack.costs
        return cls(pack.currency, crop, pack.price(crop), DRY_MATTER.get(crop, 0.86),
                   float(c["fungicide_product_ha"]), float(c["herbicide_product_ha"]),
                   float(c["drone_service_ha"]), float(c["scan_ha"]), float(c["irrigation_cm_ha"]),
                   pack.prices_assumed, pack.id)

    def revenue(self, dry_kg_ha: float) -> float:
        return self.price_per_kg * dry_kg_ha / self.dry_matter

    def planner_econ(self) -> PotatoEconomics:
        """The weekly planner's economics object (generic despite its name)."""
        return PotatoEconomics(
            price_per_kg_fresh=self.price_per_kg, dry_matter=self.dry_matter, farm_factor=1.0,
            fungicide_per_spray=self.fungicide_product_ha + self.drone_service_ha,
            scouting_per_flight=self.scan_ha,
            herbicide_blanket=self.herbicide_product_ha + self.drone_service_ha,
            herbicide_spot_fixed=0.3 * self.drone_service_ha,
            herbicide_spot_per_fraction=self.herbicide_product_ha + 0.7 * self.drone_service_ha,
            irrigation_per_cm=self.irrigation_cm_ha,
            provenance=f"region pack {self.region} ({self.currency}); "
                       + ("illustrative assumptions" if self.assumed else "sourced"))
