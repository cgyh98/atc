import pandas as pd
import numpy as np

from sklearn.cluster import DBSCAN
from geopy.distance import great_circle
from shapely.geometry import MultiPoint
from sklearn.metrics import silhouette_score
from sklearn.cluster import KMeans

from trajclus.lib.common_utils import gen_log_file
from trajclus.lib.preprocessing_lib import filter_by_airport, \
    build_flight_trajectory_df, flight_id_encoder
from trajclus.lib.geometric_utils import KM_PER_RADIAN, simplify_coordinator, \
    build_matrix_distances
from trajclus.lib.lsh_lib import LSHClusteringLib
from trajclus.lib.plot_utils import traffic_flight_plot


logger = gen_log_file(path_to_file='../tmp/lsh_clustering.log')


def dbscan_clustering(coords, min_sample=1, max_distance=1.0, epsilon=None):
    """
    Mapping all points in map to reduced cluster
    Args:
        coords :
        min_sample (int):
        max_distance (float):
        epsilon (float):

    Returns:

    """

    """
    The epsilon parameter is the max distance (max_distance) 
    that points can be from each other to be considered a cluster.
    """
    if not epsilon:
        epsilon = max_distance / KM_PER_RADIAN
    db = DBSCAN(eps=epsilon, min_samples=min_sample, algorithm='ball_tree',
                metric='haversine').fit(np.radians(coords))
    cluster_labels = db.labels_
    num_clusters = len(set(cluster_labels))
    clusters = pd.Series([coords[cluster_labels == n]
                        for n in range(num_clusters)])
    centers = clusters.map(get_centermost_point)
    centers = np.array(centers.tolist())
    print('Number of clusters for grouping: {}'.format(num_clusters))
    return centers, db


def kmeans_clustering(coords, k_cluster):
    kmeans = KMeans(n_clusters=k_cluster, random_state=0).fit(X=coords)
    cluster_labels = kmeans.labels_
    num_clusters = len(set(cluster_labels))

    centers = np.array(kmeans.cluster_centers_)

    print('Number of clusters for grouping: {}'.format(num_clusters))
    return centers, kmeans


def get_centermost_point(cluster):
    centroid = [MultiPoint(cluster).centroid.x, MultiPoint(cluster).centroid.y]
    centermost_point = min(cluster, key=lambda point: great_circle(point, centroid).m)

    return centermost_point


def compute_silhouette_score(feature_matrix, labels):
    silhouette_val = silhouette_score(
        X=feature_matrix,
        labels=labels,
        metric='precomputed'
    )
    return silhouette_val


def detect_entrance_ways(point_coords, algorithm='k-means'):
    if algorithm not in ['k-means', 'dbscan']:
        return [], False
    # auto detect entrance ways
    if algorithm == 'k-means':
        estimated_n_entrance = 9
        return kmeans_clustering(
            coords=point_coords,
            k_cluster=estimated_n_entrance
        )
    if algorithm == 'dbscan':
        return dbscan_clustering(
            coords=point_coords,
            min_sample=1,  # must be 1
            max_distance=30.0
        )


def main(input_path, airport_code='WSSS', max_flights=1000):
    # load raw-data from csv
    df = pd.read_csv(input_path)
    file_name = input_path.split("/")[-1].replace(".csv", "")

    # filter data by airport code-name
    flights_to_airport = filter_by_airport(
        df=df,
        airport_code=airport_code,
        min_dr=0.2,
        max_dr=3.0
    )
    print(flights_to_airport[['DRemains', 'Latitude', 'Longitude']].head())

    # prepare data-frame for detect entrance points toward the airport
    entrance_to_airport = filter_by_airport(
        df=df,
        airport_code=airport_code,
        min_dr=2.0,
        max_dr=3.0
    )

    logger.info("Encoding flight ID ...")
    flight_ids = flights_to_airport['Flight_ID'].unique().tolist()
    logger.info("Total # flight ID {}".format(len(flight_ids)))
    flight_encoder = flight_id_encoder(flight_ids)

    flight_df, flight_dicts = build_flight_trajectory_df(
        flights_to_airport=flights_to_airport,
        label_encoder=flight_encoder,
        flight_ids=flight_ids,
        max_flights=max_flights,
        epsilon=0.0001
    )

    entrance_trajectories = []
    for fid in flight_ids[:max_flights]:
        tmp_df = entrance_to_airport[entrance_to_airport['Flight_ID'] == fid]
        tmp_df = tmp_df.sort_values(by='DRemains', ascending=False)
        entrance_trajectories.append(tmp_df[['Latitude', 'Longitude']].values)

    simplified_coords = [simplify_coordinator(coord_curve=curve, epsilon=0.0001)
                         for curve in entrance_trajectories
                         ]

    point_coords = simplified_coords[0]
    for item in simplified_coords[1:]:
        point_coords = np.concatenate((point_coords, item))
    print("Total points at entrance %s" % len(point_coords))

    detect_entrance_algo = 'k-means'
    reduced_groups, classifier = detect_entrance_ways(
        point_coords=point_coords,
        algorithm=detect_entrance_algo
    )


    # we trick each group label as a term, then each trajectory will contains
    # list of terms/tokens
    if detect_entrance_algo == 'dbscan':
        flight_df['groups'] = [classifier.fit_predict(X=coord)
                               for coord in entrance_trajectories]
    elif detect_entrance_algo == 'k-means':
        entrance_groups = []
        for traj in entrance_trajectories:
            if len(traj) > 1:
                entrance_groups.append(classifier.predict(X=traj))
            else:
                entrance_groups.append([-1])
        flight_df['groups'] = entrance_groups

    print(flight_df.head())

    # convert clustering number to group label,
    flight_df['groups'] = flight_df['groups'].apply(
        lambda clusters: ["G{}".format(c) for c in clusters])
    print(flight_df.head())

    # Now we will apply Jaccard similarity and LSH for theses trajectories
    lsh_clustering = LSHClusteringLib(
        threshold=0.6,
        num_perm=128
    )
    flight_df['hash'] = lsh_clustering.compute_min_hash_lsh_over_data(
        record_ids=flight_df['idx'].tolist(),
        data=flight_df['groups'].tolist()
    )

    flight_df['duplicated'] = flight_df['hash'].apply(
        lambda x: lsh_clustering.query_duplicated_record(x)
    )
    print(flight_df.head())

    flight_df['buckets'] = flight_df['duplicated'].apply(
        lambda x: '_'.join(x)
    )
    print(flight_df.head())
    unique_buckets = flight_df['buckets'].unique().tolist()
    print("number buckets %s" % len(unique_buckets))
    print(unique_buckets)
    print(len(flight_df.groupby('buckets').size()))
    n_curve_per_bucket = flight_df.groupby('buckets').size().to_dict()

    def convert_to_cluster_number(bucket_label, unique_buckets, n_curve_per_bucket=None):
        if n_curve_per_bucket[bucket_label] <= 5:
            return -1
        return unique_buckets.index(bucket_label)

    cluster_labels = [
        convert_to_cluster_number(bucket, unique_buckets, n_curve_per_bucket)
        for bucket in flight_df['buckets'].tolist()
    ]
    flight_df['cluster'] = cluster_labels
    print(flight_df.head())
    print("Non-outlier cluster number %s" %
          len(flight_df[flight_df['cluster'] != -1]['cluster'].unique().tolist())
    )
    print(flight_df[flight_df['cluster'] != -1]['cluster'].unique())
    n_curve_per_cluster = flight_df.groupby('cluster').size()
    print(n_curve_per_cluster)

    # # evaluation
    silhouette_val = None
    dist_matrix = build_matrix_distances(
        coords=flight_df['trajectory'].tolist(),
        dist_type='directed_hausdorff'
    )
    silhouette_val = compute_silhouette_score(
        feature_matrix=dist_matrix, labels=cluster_labels
    )

    result_file_name =  "{file_name}_{airport_code}_lsh_sil_{subfix}.png".format(
            file_name=file_name,
            airport_code=airport_code,
            subfix=silhouette_val
        )
    traffic_flight_plot(
        flight_ids=flight_df['idx'].tolist(),
        clusters=cluster_labels,
        flight_dicts=flight_dicts,
        file_path=result_file_name,
        group_clusters=reduced_groups
    )


if __name__ == '__main__':
    main("/Users/tanthm/jvn_data/NTU/tracks_2015_06_01-001.csv")
    # main("/Users/tanthm/jvn_data/NTU/tracks_2016_09_01_destination_wsss.csv")