import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt

def run_correlation_reduction(csv_path, threshold=0.85):
    # 1. Load the dataset
    df = pd.read_csv(csv_path)
    
    # Isolate predictive features by excluding metadata and the target score
    exclude_cols = ['file_name', 'topic_id', 'rank', 'target_actionability_score']
    features = [c for c in df.columns if c not in exclude_cols]
    
    # 2. Identify and explicitly drop zero-variance features first
    variances = df[features].var()
    zero_var_cols = variances[variances == 0].index.tolist()
    features = [f for f in features if f not in zero_var_cols]
    
    print(f"Dropped Zero-Variance Features: {zero_var_cols}\n")
    
    # 3. Execute Spearman rank correlation
    corr_matrix = df[features].corr(method='spearman')
    
    # 4. Generate and save correlation heatmap
    plt.figure(figsize=(12, 10))
    sns.heatmap(corr_matrix, annot=False, cmap='coolwarm', vmin=-1, vmax=1)
    plt.title('Spearman Correlation Heatmap (Zero-Variance Dropped)')
    plt.tight_layout()
    plt.savefig('correlation_heatmap_cleaned.png')
    plt.close()
    
    # 5. Apply threshold mask and apply logical variance tie-breaker
    # Separate bounded features from raw count features for fair variance comparison
    count_features = [f for f in features if f.startswith('num_')]
    
    drop_candidates = set()
    kept_candidates = set()
    
    print(f"Highly Correlated Pairs (>= {threshold}):")
    for i in range(len(corr_matrix.columns)):
        for j in range(i+1, len(corr_matrix.columns)):
            val = corr_matrix.iloc[i, j]
            if abs(val) >= threshold:
                f1 = corr_matrix.columns[i]
                f2 = corr_matrix.columns[j]
                print(f"- {f1} & {f2}: {val:.3f}")
                
                # Intelligent Drop Logic:
                # If one is a raw count and the other is not, always drop the raw count
                if f1 in count_features and f2 not in count_features:
                    drop_candidates.add(f1)
                    kept_candidates.add(f2)
                elif f2 in count_features and f1 not in count_features:
                    drop_candidates.add(f2)
                    kept_candidates.add(f1)
                # If both are the same type, apply standard variance tie-breaker
                else:
                    if variances[f1] < variances[f2]:
                        drop_candidates.add(f1)
                        kept_candidates.add(f2)
                    else:
                        drop_candidates.add(f2)
                        kept_candidates.add(f1)

    # Resolve any conflicts where a feature is in both lists (prioritize keeping)
    final_drops = drop_candidates - kept_candidates
    final_keep = [f for f in features if f not in final_drops]
    
    print("\nFinal Recommended Drop List (Collinear / Lower Variance):")
    print(sorted(list(final_drops)))
    
    print("\nSurviving Features for Score Modeling:")
    print(sorted(final_keep))
    
    return final_keep, final_drops, zero_var_cols

# Execute the pipeline
surviving_features, dropped_features, zero_variance_features = run_correlation_reduction('flattened_topic_features.csv')