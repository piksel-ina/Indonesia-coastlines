import subprocess
import tempfile
from pathlib import Path

import boto3
import click
from fiona import listlayers
from odc.stac import configure_s3_access

from coastlines.merge_tiles import get_output_path
from coastlines.utils import (
    click_config_path,
    click_output_version,
    configure_logging,
    is_s3,
    load_config,
)


def generate_pmtiles(gpkg_path: Path, output_path: Path):
    layer_names = [l for l in listlayers(gpkg_path) if l != "layer_styles"]

    tippecanoe_layers: list[Path] = []
    for name in layer_names:
        output_geojson_path = output_path.parent / f"{output_path.stem}_{name}.geojson"
        output_pmtile_path = output_path.parent / f"{output_path.stem}_{name}.pmtiles"

        # Convert this layer to GeoJSON via GDAL (streams from GPKG, no geopandas in memory)
        subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326",
             str(output_geojson_path), str(gpkg_path), name],
            check=True,
        )

        roc_opts = " -y sig_time -y rate_time -y certainty"
        opts = {
            "hotspots_zoom_1": f"-B 0 {roc_opts}",
            "hotspots_zoom_2": f"-B 4 {roc_opts}",
            "hotspots_zoom_3": f"-B 7 {roc_opts}",
            "rates_of_change": f"-B 10 {roc_opts} -y se_time",
            "shorelines_annual": "-y year -y certainty",
        }[name]
        subprocess.run(
            ["tippecanoe", *opts.split(), "-pi", "-z13", "-f",
             "-o", str(output_pmtile_path), "-L", f"{name}:{output_geojson_path}"],
            check=True,
        )
        output_geojson_path.unlink() # Free disk as soon as tippecanoe is done with it
        tippecanoe_layers.append(output_pmtile_path)

    subprocess.run(
        ["tile-join", "-f", "-pk", "-o", str(output_path), *[str(p) for p in tippecanoe_layers]],
        check=True,
    )
    for p in tippecanoe_layers:
        p.unlink()


@click.command("create-pmtiles")
@click_config_path
@click_output_version
@click.option("--local-write", is_flag=True, default=False)
def cli(config_path, output_version, local_write):
    config = load_config(config_path, "coastlines")
    log = configure_logging()
    configure_s3_access()

    output_location = "./" if local_write else config.output.location
    input_geopackage = get_output_path(output_location, output_version, "coastlines", "gpkg")
    output_pmtiles = get_output_path(output_location, output_version, "coastlines", "pmtiles")

    with tempfile.TemporaryDirectory() as tmpdir:
        local_gpkg = f"{tmpdir}/coastlines_{output_version}.gpkg"
        local_pmtiles = f"{tmpdir}/coastlines_{output_version}.pmtiles"

        if is_s3(input_geopackage):
            boto3.client("s3").download_file(
                input_geopackage.bucket, input_geopackage.key, local_gpkg
            )
        else:
            local_gpkg = str(input_geopackage)

        log.info("Generating PMTiles")
        generate_pmtiles(Path(local_gpkg), Path(local_pmtiles))

        if is_s3(output_pmtiles):
            boto3.client("s3").upload_file(
                local_pmtiles, output_pmtiles.bucket, output_pmtiles.key
            )
            log.info(f"Wrote: s3:/{output_pmtiles}")
        else:
            Path(local_pmtiles).rename(output_pmtiles)
            log.info(f"Wrote: {output_pmtiles}")


if __name__ == "__main__":
    cli()