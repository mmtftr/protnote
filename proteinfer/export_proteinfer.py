import json
import inference
import argparse
import tensorflow as tf
import tensorflow_hub as hub
import pickle
import os

def export_model_weights(model_path:str,model_name:str,output_dir:str):
    output_path = os.path.join(output_dir,f'{model_name}_model_weights.pkl')
    module_spec=hub.saved_model_module.create_module_spec_from_saved_model(
        model_path)

    tags = [tf.saved_model.tag_constants.SERVING]
    name_scope='inferrer'
    module = hub.Module(module_spec, trainable=True, tags=tags, name=name_scope)

    with tf.Session() as sess:

        sess.run([tf.global_variables_initializer(), tf.tables_initializer()])
        
        # Fetch the trainable (weights & biases) and non trainable variables (batch norm stats)
        all_vars  = tf.global_variables()
        all_var_values = sess.run(all_vars)
        weights_dict = {var.name: value for var, value in zip(all_vars , all_var_values)}

    with open(output_path, "wb") as f:
        pickle.dump(weights_dict, f,protocol=pickle.HIGHEST_PROTOCOL)


def export_vocab(model_path:str,model_name:str,output_dir:str,vocab_variable_name:str = 'label_vocab:0'):
    inferrer = inference.Inferrer(
            savedmodel_dir_path=model_path,
            use_tqdm= True,
            batch_size=16,
            activation_type="pooled_representation"
    )
    output_path = os.path.join(output_dir,f'{model_name}_label_vocab.json')
    label_vocab = inferrer.get_variable(vocab_variable_name).astype(str)
    with open(output_path,'w') as output_file:
        json.dump(label_vocab.tolist(),output_file)

if __name__ =='__main__':
    '''
    example
    
    python proteinfer/export_proteinfer.py --model-path 'proteinfer/cached_models/noxpd2_cnn_swissprot_go_random_swiss-cnn_for_swissprot_go_random-13703706' --model-name GO

    '''
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-path',required=True,help='originally stored in cached_models after running install_models.py')
    parser.add_argument('--model-name',required=True,help='GO')
    args = parser.parse_args()
    
    HOME = os.path.abspath(os.path.join(__file__, os.pardir))
    export_folder = os.path.join(HOME,'export')
    if not os.path.exists(export_folder):
        os.mkdir(export_folder)
    
    #if os.path.exists('export')
    export_vocab(model_path=args.model_path,model_name=args.model_name,output_dir=export_folder)
    export_model_weights(model_path=args.model_path,model_name=args.model_name,output_dir=export_folder)
