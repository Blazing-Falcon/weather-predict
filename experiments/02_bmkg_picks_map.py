import geopandas as gpd
import pyarrow.parquet as pq
import shapely
from matplotlib import pyplot as plt

from nowcast.config import REPO_ROOT
from nowcast.paths import zone
from nowcast.registry.boundaries import BOUNDARIES_NAME

OUTPUT = REPO_ROOT / "docs/progress/img/step-01-bmkg-picks.png"


def main() -> None:
    plt.switch_backend("Agg")
    ref = zone("ref")
    areas = pq.read_table(ref / "dim_area.parquet").to_pandas()
    boundaries = gpd.read_file(ref / BOUNDARIES_NAME).set_index("shapeName")
    polygons = boundaries.loc[areas["geo_name"]].geometry.simplify(0.01)
    candidates = pq.read_table(ref / "bmkg_candidates.parquet", columns=["adm4", "area_id"])
    owners = candidates.to_pandas().set_index("adm4")["area_id"]
    picks = areas[areas["bmkg_adm4"].notna()]
    fallback = picks["bmkg_adm4"].map(owners) != picks["area_id"]
    inside = shapely.contains_xy(
        boundaries.loc[picks["geo_name"]].geometry.to_numpy(),
        picks["bmkg_lon"],
        picks["bmkg_lat"],
    )
    near = ~fallback & ~inside
    missing = int((areas["is_admin"] & areas["bmkg_adm4"].isna()).sum())

    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    polygons[areas["is_admin"].to_numpy()].plot(
        ax=ax, color="#f0eee9", edgecolor="#8c9299", linewidth=0.35
    )
    polygons[~areas["is_admin"].to_numpy()].plot(
        ax=ax, color="#77b5de", edgecolor="#8c9299", linewidth=0.35
    )
    for points, color, label, size in (
        (picks[inside & ~fallback], "#b4232b", "Own area inside", 7),
        (picks[fallback], "#e67e16", "Other-area fallback", 15),
        (picks[near], "#7738b7", "Near", 20),
    ):
        ax.scatter(
            points["bmkg_lon"], points["bmkg_lat"], s=size, c=color, linewidths=0, label=label
        )
    fig.suptitle(
        f"BMKG Picks  |  {len(picks)} picked  ·  {fallback.sum()} other-area  ·  "
        f"{near.sum()} near  ·  {missing} missing",
        x=0.045,
        y=0.97,
        ha="left",
        fontsize=15,
    )
    ax.legend(loc="lower left", frameon=False)
    ax.set_axis_off()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=150)
    plt.close(fig)
    print(OUTPUT.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
