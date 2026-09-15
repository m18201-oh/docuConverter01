import os
import shutil
import re
from pathlib import Path

def reorganize_images(output_dir, merged_md_file):
    """
    Collect all images from subdirectories into a central images folder.
    Update image references in the merged markdown file.
    """
    output_path = Path(output_dir)
    images_dir = output_path / "images"

    # Create images directory
    images_dir.mkdir(exist_ok=True)
    print(f"Created/verified images directory: {images_dir}")

    # Track image mappings: old_path -> new_filename
    image_mappings = {}

    # Get all subdirectories
    subdirs = sorted([d for d in output_path.iterdir() if d.is_dir() and d.name != "images"])

    for subdir in subdirs:
        folder_name = subdir.name

        # Extract page range from folder name (e.g., "01-15" from "Context Engineering_ Sessions & Memory-01-15")
        match = re.search(r'(\d+-\d+)$', folder_name)
        if match:
            prefix = match.group(1)
        else:
            # Fallback to using first 10 chars of folder name
            prefix = folder_name[:10].replace(' ', '_')

        print(f"\nProcessing folder: {folder_name}")
        print(f"  Using prefix: {prefix}")

        # Find all image files
        image_extensions = ['.jpeg', '.jpg', '.png', '.gif', '.webp']
        for ext in image_extensions:
            for image_file in subdir.glob(f"*{ext}"):
                old_relative_path = f"{folder_name}/{image_file.name}"
                new_filename = f"{prefix}_{image_file.name}"
                new_path = images_dir / new_filename

                # Copy image to central folder
                shutil.copy2(image_file, new_path)
                print(f"  Copied: {image_file.name} -> {new_filename}")

                # Store mapping
                image_mappings[old_relative_path] = f"images/{new_filename}"

    print(f"\nTotal images copied: {len(image_mappings)}")

    # Update markdown file
    print(f"\nUpdating markdown file: {merged_md_file}")
    with open(merged_md_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Replace each image reference
    for old_path, new_path in image_mappings.items():
        old_pattern = f"![]({old_path})"
        new_pattern = f"![]({new_path})"
        content = content.replace(old_pattern, new_pattern)
        print(f"  Updated: {old_path} -> {new_path}")

    # Write updated content
    with open(merged_md_file, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"\nCompleted! Markdown file updated successfully.")
    print(f"All images are now in: {images_dir}")

if __name__ == "__main__":
    output_dir = "./output"
    merged_md_file = "./output/merged_document.md"

    reorganize_images(output_dir, merged_md_file)
