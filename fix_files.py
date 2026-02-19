import os
import shutil

def fix_project_structure():
    # Get the current directory where this script is running
    base_dir = os.getcwd()
    templates_dir = os.path.join(base_dir, 'templates')

    # 1. Create templates directory if it doesn't exist
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
        print(f"Created directory: {templates_dir}")

    # 2. Move HTML files from root to templates/
    files_to_move = ['login.html', 'dashboard.html', 'order_details.html']
    
    for filename in files_to_move:
        src = os.path.join(base_dir, filename)
        dst = os.path.join(templates_dir, filename)
        
        if os.path.exists(src):
            # If destination exists, remove it first to ensure we use the new one
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
            print(f"✅ Moved {filename} to templates/ folder")
        elif os.path.exists(dst):
            print(f"ℹ️  {filename} is already in templates/ folder")
        else:
            print(f"⚠️  Warning: Could not find {filename} in root folder")

    # 3. Rename old templates to match enhanced_app.py expectations
    renames = {
        'index.html': 'order_form.html',
        'customers.html': 'orders_list.html'
    }

    for old_name, new_name in renames.items():
        old_path = os.path.join(templates_dir, old_name)
        new_path = os.path.join(templates_dir, new_name)
        
        if os.path.exists(old_path):
            if not os.path.exists(new_path):
                os.rename(old_path, new_path)
                print(f"✅ Renamed {old_name} to {new_name}")
            else:
                print(f"ℹ️  {new_name} already exists (skipping rename)")

    # 3b. Cleanup stale index.html if order_form.html exists
    if os.path.exists(os.path.join(templates_dir, 'index.html')) and os.path.exists(os.path.join(templates_dir, 'order_form.html')):
        os.remove(os.path.join(templates_dir, 'index.html'))
        print("✅ Removed stale index.html (replaced by order_form.html)")

    # 4. Create dummy files for missing templates
    missing_templates = ['customers_list.html', 'customer_details.html', 'reports.html', 'admin_orders.html']
    for template in missing_templates:
        path = os.path.join(templates_dir, template)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(f"<html><body><h1>{template}</h1><p>Placeholder</p><a href='/dashboard'>Back</a></body></html>")
            print(f"✅ Created placeholder for: {template}")

    print("\n🎉 Fix complete! You can now run 'python enhanced_app.py'")

if __name__ == "__main__":
    fix_project_structure()