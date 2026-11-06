## LLM Score modelling using LassoCV Regression technique

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error

data_csv_path = 'flattened_topic_features.csv'

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


def run_lasso_modeling(csv_path, phase_1_features):
    # 1. Load data and separate features/target
    df = pd.read_csv(csv_path)
    # exclude_cols = ['file_name', 'topic_id', 'rank', 'target_actionability_score']
    # feature_cols = [c for c in df.columns if c not in exclude_cols]

    # X = df[feature_cols]
    # y = df['target_actionability_score']

    # # Drop zero-variance columns prior to modeling
    # variances = X.var()
    # zero_var_cols = variances[variances == 0].index.tolist()
    # X = X.drop(columns=zero_var_cols)
    # feature_names = X.columns.tolist()

    X = df[phase_1_features]
    y = df['target_actionability_score']


    # 2. Compute inverse frequency sample weights (Apply sample weights to mitigate the 0.56 baseline cluster)
    # Count occurrences of each exact score
    score_counts = y.value_counts()
    # Map inverse frequencies (1 / count)
    inv_freq_map = 1.0 / score_counts
    # Normalize weights so their average is 1.0 to keep the loss function mathematically stable
    inv_freq_map = inv_freq_map / inv_freq_map.mean()
    sample_weights = y.map(inv_freq_map).values

    # 3. Standardize features (Lasso requires identical scale to apply penalty fairly)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # 4. Instantiate and Fit LassoCV independently
    # 5-fold cross-validation automatically finds the optimal L1 penalty (alpha)
    # lasso = LassoCV(cv=5, random_state=42, n_alphas=200)
    # Generates 200 log-spaced candidate alphas (1e-5 to 10), matching n_alphas=200 behavior
    lasso = LassoCV(cv=5, random_state=42, alphas=np.logspace(-5, 1, 200))
    lasso.fit(X_scaled, y, sample_weight=sample_weights)

    
    # 5. Generate predictions and evaluate performance
    predictions = lasso.predict(X_scaled)
    mse = mean_squared_error(y, predictions, sample_weight=sample_weights)
    
    # 6. Check for mathematically impossible scores
    boundary_violations = np.sum((predictions < 0.0) | (predictions > 1.0))
    
    print("--- LassoCV Program Results ---")
    print(f"Weighted MSE: {mse:.4f}")
    print(f"Boundary Violations (<0.0 or >1.0): {boundary_violations} out of {len(predictions)} predictions\n")


    # 7. Extract coefficients and identify zeroed-out dimensions
    coefficients = pd.Series(lasso.coef_, index=phase_1_features)

    zeroed_features = coefficients[coefficients == 0.0].index.tolist()
    surviving_features = coefficients[coefficients != 0.0].sort_values(key=abs, ascending=False)

    print(f"Optimal Alpha (Penalty Strength): {lasso.alpha_:.5f}\n")

    print("Zeroed-Out Features (Mathematically Irrelevant):")
    if zeroed_features:
        for f in zeroed_features:
            print(f"- {f}")
    else:
        print("- None")

    print("\nNon-Zero Feature Weights | Surviving Features (Ranked by Absolute Coefficient Weight):")
    for f, weight in surviving_features.items():
        print(f"- {f}: {weight:.4f}")

    # Optional: Plot the coefficient weights
    plt.figure(figsize=(10, 6))
    surviving_features.plot(kind='bar', color='teal')
    plt.axhline(y=0, color='black', linewidth=1)
    plt.title('LassoCV: Surviving Feature Coefficients')
    plt.ylabel('Coefficient Value (Standardized Scale)')
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    plt.savefig('lasso_coefficients.png')
    plt.close()

    return lasso, scaler, predictions, surviving_features.index.tolist(), zeroed_features


# Execute the pipeline
surviving_features, dropped_features, zero_variance_features = run_correlation_reduction(data_csv_path)
lasso_model, scaler, predictions, surviving_vars, zeroed_vars = run_lasso_modeling(data_csv_path, surviving_features)