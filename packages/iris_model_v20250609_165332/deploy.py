# Model Deployment Script
import joblib
import numpy as np

# Load model
model = joblib.load('trained_model.pkl')

# Example prediction
sample_data = np.array([[1.2, 3.4, 2.1, 0.8]])
prediction = model.predict(sample_data)
print(f"Prediction: {prediction[0]}")
