# convert_png_to_icon.py
"""
Convert your existing PNG to icon format
"""

from PIL import Image
import os

def convert_png_to_icon(input_png_path):
    """Convert PNG to icon formats for QGIS plugin"""
    
    try:
        # Check if file exists
        if not os.path.exists(input_png_path):
            print(f"Error: File not found: {input_png_path}")
            return False
            
        # Open the image
        img = Image.open(input_png_path)
        
        # Create output directory
        output_dir = os.path.dirname(input_png_path)
        
        # Save as PNG (for QGIS)
        output_png = os.path.join(output_dir, "icon.png")
        img.save(output_png, "PNG")
        print(f"PNG saved: {output_png}")
        
        # Save as ICO (for Windows)
        # Convert to appropriate sizes for ICO
        sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        icons = []
        
        for size in sizes:
            # Resize image
            resized = img.resize(size, Image.Resampling.LANCZOS)
            icons.append(resized)
        
        # Save as ICO
        output_ico = os.path.join(output_dir, "icon.ico")
        icons[0].save(output_ico, format="ICO", sizes=sizes, append_images=icons[1:])
        print(f"ICO saved: {output_ico}")
        
        print("\nIcon conversion complete!")
        print(f"Files created:")
        print(f"  - {output_png}")
        print(f"  - {output_ico}")
        
        return True
        
    except Exception as e:
        print(f"Error converting icon: {e}")
        return False

if __name__ == "__main__":
    # Ask for PNG file path
    png_path = input("Enter the full path to your PNG file: ").strip().strip('"')
    convert_png_to_icon(png_path)