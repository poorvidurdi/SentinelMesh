import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np

def generate_graphs():
    os.makedirs("results", exist_ok=True)
    
    if not os.path.exists("data/metrics.csv"):
        print("[Experiments] data/metrics.csv not found. Run the simulation first.")
        return
        
    df = pd.read_csv("data/metrics.csv")
    if len(df) == 0:
        print("[Experiments] data/metrics.csv is empty. Run the simulation first.")
        return
        
    df['timestamp'] = df['timestamp'] - df['timestamp'].min()
    
    # 1. Battery Drain Over Time
    plt.figure(figsize=(10, 6))
    for nid in sorted(df['node_id'].unique()):
        ndf = df[df['node_id'] == nid]
        plt.plot(ndf['timestamp'], ndf['battery'], label=f'Node {nid}')
    plt.xlabel('Time (s)')
    plt.ylabel('Battery Level (%)')
    plt.title('Node Battery Degradation Over Time')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig('results/experiment1_battery.png', dpi=300)
    plt.close()
    
    # 2. Packet Loss Spike Analysis
    plt.figure(figsize=(10, 6))
    sns.lineplot(data=df, x='timestamp', y='packet_loss', hue='node_id', palette='tab10')
    plt.xlabel('Time (s)')
    plt.ylabel('Packet Loss (%)')
    plt.title('Packet Loss Variation and Failure Spikes')
    plt.grid(True, alpha=0.3)
    plt.savefig('results/experiment2_packet_loss.png', dpi=300)
    plt.close()

    # 3. Label Distribution (Healthy vs Pre-failure)
    plt.figure(figsize=(8, 6))
    sns.countplot(data=df, x='label', palette=['#00e676', '#ffab00'])
    plt.title('Distribution of Node Health States')
    plt.ylabel('Count')
    plt.savefig('results/experiment3_health_states.png', dpi=300)
    plt.close()

    # 4. Correlation Heatmap
    plt.figure(figsize=(8, 6))
    if len(df) > 1:
        corr = df[['battery', 'packet_loss']].corr()
        sns.heatmap(corr, annot=True, cmap='coolwarm', vmin=-1, vmax=1)
        plt.title('Correlation Matrix: Battery vs Packet Loss')
        plt.savefig('results/experiment4_correlation.png', dpi=300)
    plt.close()

    # 5. Scatter Plot of Failure Region
    plt.figure(figsize=(10, 6))
    if len(df['label'].unique()) > 1:
        sns.scatterplot(data=df, x='battery', y='packet_loss', hue='label', palette=['#00e676', '#ffab00'], alpha=0.7)
    else:
        sns.scatterplot(data=df, x='battery', y='packet_loss', alpha=0.7)
    plt.xlabel('Battery Level (%)')
    plt.ylabel('Packet Loss (%)')
    plt.title('ML Decision Boundary Visualization')
    plt.grid(True, alpha=0.3)
    plt.savefig('results/experiment5_boundary.png', dpi=300)
    plt.close()

    print("[Experiments] 5 experiment graphs successfully generated in results/")

if __name__ == "__main__":
    generate_graphs()
